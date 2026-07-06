"""`owner_guild_only` forward gate — Part B / B2, spec §4.2.

`_capture_and_forward` (`controller.py`) gates on `cfg.owner_guild_only`
AFTER the self-filter and BEFORE register/forward: with it on, only the
owner's guilds (+ owner DMs) reach the daemon; a stranger DM, a guild the
owner isn't a member of, an event with no resolvable guild at all, or an
`is_owner_in_guild` call that raises are all dropped fail-closed. `Ambient
Log.append` is unconditional and runs BEFORE this gate (spec §3.3 C3) — every
case below, forwarded or dropped, must still produce an ambient log line.

A per-guild TTL cache (`_owner_in_guild_cached`) sits in front of
`DiscordClient.is_owner_in_guild` so a burst of messages in the same guild
doesn't re-fetch membership on every single one.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import pytest

from dollos.discord_bridge.__main__ import _load_bridge_config
from dollos.discord_bridge.ambient_log import AmbientLog
from dollos.discord_bridge.controller import BridgeConfig, BridgeController
from dollos.ipc.messages import ChannelEvent, ChannelRegister


class FakeDiscordClient:
    """`DiscordClient` double with a configurable `is_owner_in_guild`.

    `set_owner_guild(guild_id, is_member)` seeds membership; `raise_on_guild
    (guild_id)` arms `is_owner_in_guild` to raise for that guild on its next
    (and only its next, until re-armed) call — simulating the "transient
    failure" fail-closed path. `is_owner_in_guild_calls` records every
    guild_id `is_owner_in_guild` was actually called with, so tests can
    assert the TTL cache suppressed a repeat fetch.
    """

    def __init__(self, *, bot_id: str = "bot-999") -> None:
        self._bot_id = bot_id
        self.sent: list[tuple[str, str]] = []
        self._cb: Callable[[dict], Awaitable[None]] | None = None
        self._owner_guilds: set[str] = set()
        self._raise_for_guilds: set[str] = set()
        self.is_owner_in_guild_calls: list[str] = []

    def on_message(self, cb: Callable[[dict], Awaitable[None]]) -> None:
        self._cb = cb

    async def send(self, channel_id: str, text: str) -> None:
        self.sent.append((channel_id, text))

    def me_id(self) -> str:
        return self._bot_id

    async def run(self) -> None:
        raise NotImplementedError("not exercised by gate tests")

    async def fetch_history(self, channel_id: str, limit: int) -> list[dict]:
        return []

    def set_owner_guild(self, guild_id: str, is_member: bool) -> None:
        if is_member:
            self._owner_guilds.add(guild_id)
        else:
            self._owner_guilds.discard(guild_id)

    def raise_on_guild(self, guild_id: str) -> None:
        self._raise_for_guilds.add(guild_id)

    async def is_owner_in_guild(self, guild_id: str, owner_id: str) -> bool:
        self.is_owner_in_guild_calls.append(guild_id)
        if guild_id in self._raise_for_guilds:
            raise RuntimeError("simulated is_owner_in_guild failure")
        return guild_id in self._owner_guilds


def _event(**kw) -> dict:
    base = dict(
        author_id="42", author="stranger", is_dm=False, mentioned=False,
        content="hi", channel_id="c1", guild="g1", channel="general",
        msg_id="m1",
    )
    base.update(kw)
    return base


def _cfg(**kw) -> BridgeConfig:
    base = dict(owner_id="owner-1", bot_id="bot-999")
    base.update(kw)
    return BridgeConfig(**base)


def _make(tmp_path, **cfg_kw):
    discord = FakeDiscordClient(bot_id=cfg_kw.pop("bot_id", "bot-999"))
    sent_to_daemon: list[object] = []

    async def daemon_send(msg: object) -> None:
        sent_to_daemon.append(msg)

    ambient = AmbientLog(tmp_path, retention_days=30)
    cfg = _cfg(bot_id=discord.me_id(), **cfg_kw)
    controller = BridgeController(discord, daemon_send, ambient, cfg)
    return controller, discord, sent_to_daemon, ambient


def _ambient_lines(tmp_path, guild_id: str, channel_id: str) -> list[dict]:
    files = list((tmp_path / "discord" / guild_id / channel_id).glob("*.jsonl"))
    assert len(files) == 1, f"expected exactly one ambient log file, found {files}"
    return [json.loads(line) for line in files[0].read_text().splitlines()]


# ----- owner_guild_only=true (BridgeConfig default) -----


async def test_owner_guild_message_is_forwarded(tmp_path):
    controller, discord, sent, ambient = _make(tmp_path)
    discord.set_owner_guild("g1", True)

    await controller.on_discord_message(_event())

    events = [m for m in sent if isinstance(m, ChannelEvent)]
    assert len(events) == 1
    assert events[0].payload["msg_id"] == "m1"
    assert _ambient_lines(tmp_path, "g1", "c1")[0]["msg_id"] == "m1"


async def test_non_owner_guild_message_is_dropped_but_still_logged(tmp_path):
    controller, discord, sent, ambient = _make(tmp_path)
    # owner NOT seeded as a member of g1 -> is_owner_in_guild returns False.

    await controller.on_discord_message(_event())

    assert sent == []
    assert _ambient_lines(tmp_path, "g1", "c1")[0]["msg_id"] == "m1"


async def test_owner_dm_is_forwarded(tmp_path):
    controller, discord, sent, ambient = _make(tmp_path)

    await controller.on_discord_message(
        _event(
            is_dm=True, guild=None, channel_id="dm-1",
            author_id="owner-1", msg_id="m2",
        )
    )

    events = [m for m in sent if isinstance(m, ChannelEvent)]
    assert len(events) == 1
    assert events[0].payload["msg_id"] == "m2"
    assert _ambient_lines(tmp_path, "dm", "dm-1")[0]["msg_id"] == "m2"
    # DM path never touches is_owner_in_guild (short-circuits on author_id).
    assert discord.is_owner_in_guild_calls == []


async def test_stranger_dm_is_dropped_but_still_logged(tmp_path):
    controller, discord, sent, ambient = _make(tmp_path)

    await controller.on_discord_message(
        _event(
            is_dm=True, guild=None, channel_id="dm-1",
            author_id="stranger-1", msg_id="m3",
        )
    )

    assert sent == []
    assert _ambient_lines(tmp_path, "dm", "dm-1")[0]["msg_id"] == "m3"
    assert discord.is_owner_in_guild_calls == []


async def test_is_owner_in_guild_raising_drops_fail_closed(tmp_path):
    controller, discord, sent, ambient = _make(tmp_path)
    discord.raise_on_guild("g1")

    await controller.on_discord_message(_event())

    assert sent == []
    assert _ambient_lines(tmp_path, "g1", "c1")[0]["msg_id"] == "m1"


async def test_guild_none_on_non_dm_event_is_dropped_but_still_logged(tmp_path):
    """A malformed/edge event that isn't flagged is_dm but has no guild at
    all must fail-closed drop rather than crash or forward."""
    controller, discord, sent, ambient = _make(tmp_path)

    await controller.on_discord_message(_event(guild=None))

    assert sent == []
    assert _ambient_lines(tmp_path, "dm", "c1")[0]["msg_id"] == "m1"


# ----- owner_guild_only=false: forward-all (current/legacy behavior) -----


async def test_owner_guild_only_false_forwards_everything(tmp_path):
    controller, discord, sent, ambient = _make(tmp_path, owner_guild_only=False)
    # Neither seeded as an owner guild nor an owner DM -> would drop under
    # owner_guild_only=True, but must forward here.

    await controller.on_discord_message(_event(msg_id="m1"))
    await controller.on_discord_message(
        _event(
            is_dm=True, guild=None, channel_id="dm-1",
            author_id="stranger-1", msg_id="m2",
        )
    )

    events = [m for m in sent if isinstance(m, ChannelEvent)]
    assert [e.payload["msg_id"] for e in events] == ["m1", "m2"]
    assert discord.is_owner_in_guild_calls == []  # gate never runs when off


# ----- TTL cache -----


async def test_owner_in_guild_result_cached_within_ttl(tmp_path):
    controller, discord, sent, ambient = _make(tmp_path)
    discord.set_owner_guild("g1", True)

    await controller.on_discord_message(_event(msg_id="m1"))
    await controller.on_discord_message(_event(msg_id="m2"))

    assert discord.is_owner_in_guild_calls == ["g1"]  # fetched once, cached second time
    events = [m for m in sent if isinstance(m, ChannelEvent)]
    assert [e.payload["msg_id"] for e in events] == ["m1", "m2"]


async def test_owner_in_guild_cache_is_per_guild(tmp_path):
    controller, discord, sent, ambient = _make(tmp_path)
    discord.set_owner_guild("g1", True)
    discord.set_owner_guild("g2", False)

    await controller.on_discord_message(_event(guild="g1", msg_id="m1"))
    await controller.on_discord_message(
        _event(guild="g2", channel_id="c2", msg_id="m2")
    )

    assert discord.is_owner_in_guild_calls == ["g1", "g2"]
    events = [m for m in sent if isinstance(m, ChannelEvent)]
    assert [e.payload["msg_id"] for e in events] == ["m1"]  # only g1 forwarded


# ----- config-load guard (spec I1) -----


def _write_toml(tmp_path, body: str):
    path = tmp_path / "bridge.toml"
    path.write_text(body)
    return path


def test_load_bridge_config_raises_when_owner_guild_only_true_and_owner_id_empty(
    tmp_path,
):
    path = _write_toml(
        tmp_path,
        """
[discord]
token = "tok"
owner_discord_id = ""
channel_allowlist = ["c1"]
owner_guild_only = true
""",
    )
    with pytest.raises(ValueError, match="owner_guild_only"):
        _load_bridge_config(path)


def test_load_bridge_config_defaults_owner_guild_only_true(tmp_path):
    path = _write_toml(
        tmp_path,
        """
[discord]
token = "tok"
owner_discord_id = "111"
channel_allowlist = ["c1"]
""",
    )
    token, cfg = _load_bridge_config(path)
    assert cfg.owner_guild_only is True


def test_load_bridge_config_honors_explicit_owner_guild_only_false(tmp_path):
    path = _write_toml(
        tmp_path,
        """
[discord]
token = "tok"
owner_discord_id = "111"
channel_allowlist = ["c1"]
owner_guild_only = false
""",
    )
    token, cfg = _load_bridge_config(path)
    assert cfg.owner_guild_only is False


def test_load_bridge_config_owner_guild_only_false_allows_empty_owner_id(tmp_path):
    """The guard only fires when owner_guild_only=True — false with an
    empty owner_id is unsafe in its own way (any DM wakes her, unrelated to
    this spec — see D7 in the design doc) but is not this task's concern and
    must not be blocked here."""
    path = _write_toml(
        tmp_path,
        """
[discord]
token = "tok"
owner_discord_id = ""
channel_allowlist = ["c1"]
owner_guild_only = false
""",
    )
    token, cfg = _load_bridge_config(path)
    assert cfg.owner_guild_only is False
    assert cfg.owner_id == ""
