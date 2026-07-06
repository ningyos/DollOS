"""`_load_bridge_config` (discord_bridge/__main__.py) — Part A A5 + Part B B3.

`name_aliases` / `always_wake_channels` are dead bridge config (P1c moved
L0/L1 wake admission daemon-side into `AttentionGate`; A3 confirmed the
`alias_provider` is built kernel-side from pack seeds + daemon config floor
+ learned tokens — the bridge never participates). A5 removes both fields
from `BridgeConfig` and stops `_load_bridge_config` from reading them.

`channel_allowlist` is ALSO dead bridge config (2026-07-06 spec §4.3, Part
B / B3): it never gated forwarding (P1c already made the bridge
forward-all; B2's `owner_guild_only` is the real gate) — its only two uses
(seeding reply-routing pre-register + the reconnect-backfill channel list)
are superseded by register-on-first-forward and the new, OPTIONAL
`backfill_channels` field respectively. B3 removes `channel_allowlist` from
`BridgeConfig` and stops `_load_bridge_config` from reading it, the same
way A5 did for the other two dead fields.

Per the plan's constraint: `_load_bridge_config` must NOT reject a
`bridge.toml` that still has a leftover `name_aliases`/`always_wake_channels`/
`channel_allowlist` key (users may have an old file lying around) — it just
stops reading them. `tomllib.load` + `dict.get`/indexing on the fields we DO
want means extra keys are silently ignored; no strict-key validation is
added (spec: no fallback logic, but this isn't a fallback — it's just not
reading a key that no longer maps to any field).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from dollos.discord_bridge import __main__ as main_module
from dollos.discord_bridge.__main__ import _load_bridge_config
from dollos.discord_bridge.ambient_log import AmbientLog
from dollos.discord_bridge.controller import BridgeConfig


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "bridge.toml"
    path.write_text(body)
    return path


def test_load_bridge_config_without_dead_keys(tmp_path: Path):
    """A bridge.toml with none of the dead keys loads fine — the common
    case post-B3 (bridge.example.toml no longer has any of them)."""
    path = _write(
        tmp_path,
        """
[discord]
token = "tok-1"
owner_discord_id = "111"
""",
    )
    token, cfg = _load_bridge_config(path)
    assert token == "tok-1"
    assert isinstance(cfg, BridgeConfig)
    assert cfg.owner_id == "111"
    assert cfg.backfill_channels == []
    assert not hasattr(cfg, "channel_allowlist")
    assert not hasattr(cfg, "name_aliases")
    assert not hasattr(cfg, "always_wake_channels")


def test_load_bridge_config_ignores_leftover_dead_keys(tmp_path: Path):
    """A bridge.toml carrying OLD `channel_allowlist` / `name_aliases` /
    `always_wake_channels` keys (a pre-B3 file a user hasn't regenerated)
    must not crash the loader — the keys are just never read, not
    validated against."""
    path = _write(
        tmp_path,
        """
[discord]
token = "tok-2"
owner_discord_id = "222"
channel_allowlist = ["c1"]
name_aliases = ["Gura", "gura", "古拉"]
always_wake_channels = ["vip"]
""",
    )
    token, cfg = _load_bridge_config(path)
    assert token == "tok-2"
    assert cfg.owner_id == "222"
    assert not hasattr(cfg, "channel_allowlist")
    assert cfg.backfill_channels == []


def test_load_bridge_config_reads_backfill_channels(tmp_path: Path):
    """`backfill_channels` (2026-07-06 spec §4.3, Part B / B3, D5(ii)) is
    OPTIONAL and, when present, is read verbatim onto `BridgeConfig` —
    decoupled from `owner_guild_only`'s wake scope."""
    path = _write(
        tmp_path,
        """
[discord]
token = "tok-3"
owner_discord_id = "333"
backfill_channels = ["c1", "c2"]
""",
    )
    token, cfg = _load_bridge_config(path)
    assert cfg.backfill_channels == ["c1", "c2"]


def test_load_bridge_config_defaults_backfill_channels_to_empty(tmp_path: Path):
    """No `backfill_channels` key at all -> `[]`, i.e. no reconnect-gap
    backfill — the safe/predictable default (D5(ii))."""
    path = _write(
        tmp_path,
        """
[discord]
token = "tok-4"
owner_discord_id = "444"
""",
    )
    token, cfg = _load_bridge_config(path)
    assert cfg.backfill_channels == []


# ----- M1 (Part B whole-branch review): no redundant pre-register loop -----
#
# `_connect_and_run` used to pre-register every `backfill_channels` entry by
# sending a `ChannelRegister` directly over the raw `ws`, ahead of
# `controller.reconnect_backfill`. Because `BridgeController._registered`
# starts empty (2026-07-06 spec §4.3, Part B / B3) and that direct send
# never touched it, the backfilled channel's first replayed event then hit
# register-on-first-forward in `_capture_and_forward` and sent a SECOND
# `ChannelRegister` for the same channel — two registrations per backfilled
# channel per reconnect, orphaning one `SinkResolver` handle each time. The
# fix deletes the redundant loop outright (the real backfill purpose —
# `fetch_history` replay — is untouched). These fakes drive
# `_connect_and_run` end to end (daemon WS + Discord client) to prove the
# fix: history is still backfilled, and exactly one `ChannelRegister` is
# sent for a `backfill_channels` entry that then forwards.


class _FakeDiscordClient:
    """Minimal stand-in for `PycordClient` — only what `_connect_and_run`
    touches: `on_message`, `run`, `wait_until_ready` (PycordClient-specific,
    not on the `DiscordClient` Protocol), `me_id`, `fetch_history`."""

    def __init__(self, *, token: str) -> None:
        self.token = token
        self._cb = None

    def on_message(self, cb) -> None:
        self._cb = cb

    async def run(self) -> None:
        # Never resolves on its own — `_connect_and_run`'s `finally` cancels
        # this task once the daemon WS loop below ends.
        await asyncio.Event().wait()

    async def wait_until_ready(self) -> None:
        return None

    def me_id(self) -> str:
        return "bot-1"

    async def fetch_history(self, channel_id: str, limit: int) -> list[dict]:
        if channel_id != "c1":
            return []
        return [
            {
                "author_id": "owner-1", "author": "owner", "is_dm": False,
                "mentioned": False, "content": "backfilled msg",
                "channel_id": "c1", "guild": "g1", "channel": "general",
                "msg_id": "m1",
            }
        ]


class _FakeWS:
    """Records everything sent; the `async for raw in ws` receive loop in
    `_connect_and_run` ends immediately (no incoming daemon messages) so the
    function returns as soon as backfill + task-cancel finish."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _FakeConnect:
    def __init__(self, ws: _FakeWS) -> None:
        self._ws = ws

    async def __aenter__(self) -> _FakeWS:
        return self._ws

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def test_connect_and_run_registers_backfill_channel_exactly_once(
    tmp_path, monkeypatch
):
    """End-to-end (minus real network/Discord): a `backfill_channels`
    reconnect must still replay history (real backfill purpose, untouched)
    AND must send exactly one `ChannelRegister` for that channel — not the
    two the old pre-register loop produced."""
    fake_ws = _FakeWS()
    monkeypatch.setattr(
        main_module.websockets, "connect", lambda url: _FakeConnect(fake_ws)
    )
    monkeypatch.setattr(main_module, "PycordClient", _FakeDiscordClient)

    cfg = BridgeConfig(
        owner_id="owner-1", owner_guild_only=False, backfill_channels=["c1"]
    )
    ambient = AmbientLog(tmp_path, retention_days=30)
    args = main_module.build_parser().parse_args(
        ["--config", str(tmp_path / "unused.toml")]
    )

    await main_module._connect_and_run(args, "tok", cfg, ambient)

    sent = [json.loads(m) for m in fake_ws.sent]
    events = [m for m in sent if m.get("type") == "channel_event"]
    registers = [m for m in sent if m.get("type") == "channel_register"]
    c1_registers = [m for m in registers if m.get("channel_id") == "c1"]

    # Backfill history still replays (fetch_history's one event forwards).
    assert len(events) == 1
    assert events[0]["payload"]["msg_id"] == "m1"
    # Exactly one ChannelRegister for c1 — register-on-first-forward only,
    # no redundant pre-register duplicate.
    assert len(c1_registers) == 1
