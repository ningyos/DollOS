"""Discord bridge CLI.

Usage:
    python -m dollos.discord_bridge --daemon ws://127.0.0.1:9876 \\
        --config discord_bridge.toml

Connects to the daemon as a WS client, optionally pre-registers each
`backfill_channels` entry (`ChannelRegister`, spec §4.3, Part B / B3 — most
deployments leave this empty and rely on register-on-first-forward
instead), and wires `PycordClient` <-> daemon through `BridgeController`.
Mirrors `src/dollos/voice/bridge/__main__.py`'s shape
(argparse + `websockets.connect` + background client task). Kept thin on
purpose — all business logic (full-capture, L0 wake, reply routing) lives in
`BridgeController`; this module is untested wiring (see task-5-report.md).

`run()` wraps one daemon+Discord connection lifecycle (`_connect_and_run`) in
a reconnect loop (Task 7): any dropped connection — daemon WS closing,
Discord disconnecting, or simply the first connect after a process
restart/kill — re-enters `_connect_and_run`, which always calls
`controller.reconnect_backfill` right after connecting to catch up on
whatever gap preceded it — scoped to `cfg.backfill_channels` (2026-07-06
spec §4.3, Part B / B3, D5(ii)); empty (the default) means this call fetches
no history at all, relying on live traffic to resume immediately instead.
This is what makes the "reconnect after a kill → backfill dedups"
live-smoke case (P1b gate) hold when `backfill_channels` IS set: a
killed-and-relaunched bridge process is just loop iteration 1 of a fresh
`run()` call.

`[discord]` TOML config shape (spec §3.1 §5.3, §4.3):
    token = "..."                    # bot token, on-device, not in git
    owner_discord_id = "123..."      # numeric Discord user id
    owner_guild_only = true          # optional, default true (spec §4.2, Part B / B2)
    backfill_channels = ["111", "222"]  # optional, default [] (spec §4.3, Part B / B3, D5(ii))

`name_aliases` / `always_wake_channels` used to live here too, but were
removed (2026-07-06 self-learned-aliases spec §3.6, Part A A5) — both were
dead config once P1c moved L0/L1 wake admission daemon-side into
`AttentionGate`. `channel_allowlist` was also removed (2026-07-06 spec
§4.3, Part B / B3) — it never gated forwarding either; its only uses
(seeding reply-routing pre-register + providing the reconnect-backfill
channel list) are now covered by register-on-first-forward and the
optional `backfill_channels` above, respectively. A leftover
`name_aliases`/`always_wake_channels`/`channel_allowlist` key in an old
`bridge.toml` is harmless: `_load_bridge_config` below simply never reads
any of them anymore (no strict-key validation is added on top of
`tomllib.load`).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tomllib
from pathlib import Path

import websockets

from dollos.discord_bridge.ambient_log import AmbientLog
from dollos.discord_bridge.client import PycordClient
from dollos.discord_bridge.controller import BridgeConfig, BridgeController
from dollos.ipc.messages import AddressedText, ChannelRegister

logger = logging.getLogger("dollos.discord_bridge")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dollos.discord_bridge",
        description="Discord bridge — connects a Discord bot to DollOS",
    )
    p.add_argument(
        "--daemon", type=str, default="ws://127.0.0.1:9876",
        help="Daemon WS URL (default ws://127.0.0.1:9876)",
    )
    p.add_argument(
        "--config", type=Path, required=True,
        help="Path to the bridge's [discord] TOML config (token, "
             "owner_discord_id, owner_guild_only)",
    )
    p.add_argument(
        "--data-root", type=Path, default=Path("data"),
        help="data root for the ambient log (default ./data)",
    )
    p.add_argument(
        "--retention-days", type=int, default=30,
        help="ambient log retention in days (default 30)",
    )
    p.add_argument("--verbose", action="store_true")
    return p


def _load_bridge_config(path: Path) -> tuple[str, BridgeConfig]:
    """Load the `[discord]` table from `path`. Returns (token, BridgeConfig).

    `owner_guild_only` (2026-07-06 spec §4.2, Part B / B2, D7) defaults to
    `True` — safe-by-default: only the owner's guilds (+ owner DMs) forward
    to the daemon. Config-load guard (spec I1): a `True` `owner_guild_only`
    with an empty `owner_id` has no safe semantics — a gate that only
    trusts "the owner" but doesn't know who that is would either drop
    everything or (if some future code path treated "unknown owner" as
    "trust nobody restrictions" loosely) leak. Refuse to start rather than
    guess either way.

    `channel_allowlist` is no longer read here (2026-07-06 spec §4.3, Part
    B / B3) — a leftover `channel_allowlist` key in an old `bridge.toml`
    is simply ignored, not rejected. `backfill_channels` (D5(ii)) is the
    OPTIONAL replacement for backfill scope specifically — defaults to `[]`
    (no reconnect-gap backfill at all) when absent.
    """
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    d = raw["discord"]
    owner_id = str(d["owner_discord_id"])
    owner_guild_only = d.get("owner_guild_only", True)
    if owner_guild_only and not owner_id:
        raise ValueError(
            "bridge config: owner_guild_only=true requires a non-empty "
            "owner_discord_id — refusing to start with a gate that would "
            "trust no one (set owner_discord_id, or set "
            "owner_guild_only=false if you really want forward-all)."
        )
    cfg = BridgeConfig(
        owner_id=owner_id,
        owner_guild_only=owner_guild_only,
        backfill_channels=list(d.get("backfill_channels", [])),
    )
    return d["token"], cfg


async def run(args: argparse.Namespace) -> None:
    """Never returns under normal operation — this is a long-running
    service loop (Task 7 reconnect loop below); it only exits via an
    unhandled `KeyboardInterrupt`/`CancelledError` propagating out."""
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    token, cfg = _load_bridge_config(args.config)
    ambient = AmbientLog(args.data_root, retention_days=args.retention_days)

    # Reconnect loop (Task 7): a dropped daemon WS or Discord connection —
    # or simply the first connect after a process kill+relaunch — re-enters
    # _connect_and_run, which always backfills the gap before serving live
    # traffic again. No separate "shutdown" flag exists yet (this CLI has no
    # graceful-stop signal beyond process kill / Ctrl+C, which raises
    # KeyboardInterrupt — not an Exception subclass, so it is NOT swallowed
    # by the `except Exception` below and propagates out of the loop).
    reconnect_delay_s = 5.0
    while True:
        try:
            await _connect_and_run(args, token, cfg, ambient)
        except Exception:
            logger.exception(
                "discord bridge connection dropped — reconnecting in %.0fs",
                reconnect_delay_s,
            )
        else:
            logger.warning(
                "daemon connection closed cleanly — reconnecting in %.0fs",
                reconnect_delay_s,
            )
        await asyncio.sleep(reconnect_delay_s)


async def _connect_and_run(
    args: argparse.Namespace,
    token: str,
    cfg: BridgeConfig,
    ambient: AmbientLog,
) -> None:
    """One daemon+Discord connection lifecycle: connect to both, backfill
    the reconnect gap, then serve live traffic until the daemon WS closes or
    an error propagates (`run()`'s reconnect loop calls this again)."""
    discord = PycordClient(token=token)

    logger.info("connecting to daemon: %s", args.daemon)
    async with websockets.connect(args.daemon) as ws:

        async def daemon_send(msg) -> None:
            await ws.send(msg.model_dump_json())

        controller = BridgeController(discord, daemon_send, ambient, cfg)

        async def _on_discord_message(event: dict) -> None:
            # PycordClient.me_id() only resolves once connected (raises
            # RuntimeError before — see client.py); by the time the first
            # event reaches here the client has connected, so this lazily
            # fills cfg.bot_id exactly once. There is no separate "ready"
            # hook on the DiscordClient Protocol to do this earlier.
            if cfg.bot_id is None:
                cfg.bot_id = discord.me_id()
            await controller.on_discord_message(event)

        discord.on_message(_on_discord_message)

        # Pre-register is now OPTIONAL (2026-07-06 spec §4.3, Part B / B3):
        # under forward-all + register-on-first-forward
        # (`BridgeController._capture_and_forward`), pre-registering a
        # channel here is never load-bearing for correctness — the first
        # inbound (or backfilled) message from ANY channel registers it
        # dynamically before forwarding. This loop only pre-registers
        # `backfill_channels`, if the operator set any, so their reconnect
        # history (below) has a sink to route through immediately rather
        # than waiting on register-on-first-forward during the backfill
        # replay itself. Empty `backfill_channels` (the default) means this
        # loop registers nothing on connect at all.
        for channel_id in cfg.backfill_channels:
            await ws.send(
                ChannelRegister(
                    channel_id=channel_id, locus="external", kind="discord",
                ).model_dump_json()
            )
        if cfg.backfill_channels:
            logger.info(
                "pre-registered %d backfill channel(s)",
                len(cfg.backfill_channels),
            )

        discord_task = asyncio.create_task(
            discord.run(), name="discord-bridge-client"
        )

        try:
            # wait_until_ready() (PycordClient-specific, not on the
            # DiscordClient Protocol) is the "successful reconnect" signal:
            # only once py-cord's cache is ready can fetch_history() resolve
            # channels, so backfill runs right after, before live-message
            # processing below.
            await discord.wait_until_ready()
            if cfg.bot_id is None:
                cfg.bot_id = discord.me_id()
            logger.info("discord connected — backfilling reconnect gap")
            await controller.reconnect_backfill(
                discord.fetch_history, cfg.backfill_channels
            )

            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                data = json.loads(raw)
                if data.get("type") == "addressed_text":
                    await controller.on_daemon_message(
                        AddressedText.model_validate(data)
                    )
                # Other server message types (TextChunk/TurnEnd/...) are not
                # meaningful on a Discord-only bridge connection — ignored.
        finally:
            discord_task.cancel()
            try:
                await discord_task
            except (asyncio.CancelledError, Exception):
                pass


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
