"""Discord bridge CLI.

Usage:
    python -m dollos.discord_bridge --daemon ws://127.0.0.1:9876 \\
        --config discord_bridge.toml

Connects to the daemon as a WS client, registers each allowlisted channel
(`ChannelRegister`, spec §3.1), and wires `PycordClient` <-> daemon through
`BridgeController`. Mirrors `src/dollos/voice/bridge/__main__.py`'s shape
(argparse + `websockets.connect` + background client task). Kept thin on
purpose — all business logic (full-capture, L0 wake, reply routing) lives in
`BridgeController`; this module is untested wiring (see task-5-report.md).

`[discord]` TOML config shape (spec §3.1 §5.3):
    token = "..."                    # bot token, on-device, not in git
    owner_discord_id = "123..."      # numeric Discord user id
    name_aliases = ["gura", "古拉"]
    channel_allowlist = ["111", "222"]
    always_wake_channels = []        # optional, defaults to []
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
             "owner_discord_id, name_aliases, channel_allowlist, "
             "always_wake_channels)",
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
    """Load the `[discord]` table from `path`. Returns (token, BridgeConfig)."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    d = raw["discord"]
    cfg = BridgeConfig(
        owner_id=str(d["owner_discord_id"]),
        name_aliases=list(d.get("name_aliases", [])),
        always_wake_channels=set(d.get("always_wake_channels", [])),
        channel_allowlist=list(d["channel_allowlist"]),
    )
    return d["token"], cfg


async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    token, cfg = _load_bridge_config(args.config)
    ambient = AmbientLog(args.data_root, retention_days=args.retention_days)
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

        for channel_id in cfg.channel_allowlist:
            await ws.send(
                ChannelRegister(
                    channel_id=channel_id, locus="external", kind="discord",
                ).model_dump_json()
            )
        logger.info(
            "registered %d allowlisted channel(s)", len(cfg.channel_allowlist)
        )

        discord_task = asyncio.create_task(
            discord.run(), name="discord-bridge-client"
        )

        try:
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

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
