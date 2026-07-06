"""Bridge forward-all (P1c Task 3, Option A — spec §3.4 "全量訊息都送 daemon").

P1b's `_capture_and_maybe_wake` ran `l0_wake` before forwarding: only
wake-worthy messages (DM / mention / name-alias / always-wake channel)
became a `ChannelEvent`; everything else was ambient-log-only. P1c moves
L0/L1 admission daemon-side into `AttentionGate` (Task 1/2/4) — the bridge
no longer decides wake locally. It becomes a dumb forwarder:

- `AmbientLog.append` still fires for EVERY message (corpus completeness),
  including the bot's own.
- The self-filter stays: her own messages (`author_id == bot_id`) are
  logged but never forwarded — this is the ONLY gate left on the bridge
  side. Losing it would re-introduce the self-echo infinite loop P1b fixed.
- Every other (non-self) allowlist-channel message is forwarded to the
  daemon as a `ChannelEvent`, regardless of whether it carries any L0
  signal — the daemon's `AttentionGate` decides admission now.
- Dynamic `ChannelRegister` (P1b: register a channel's external sink the
  first time it produces daemon traffic) now fires on first FORWARD
  instead of first WAKE — there's no more local wake concept to hang it on.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from dollos.discord_bridge.ambient_log import AmbientLog
from dollos.discord_bridge.controller import BridgeConfig, BridgeController
from dollos.ipc.messages import ChannelEvent, ChannelRegister


class FakeDiscordClient:
    """Minimal `DiscordClient` double — forward-all no longer depends on
    any wake-rule behavior from the client, so this only needs to satisfy
    the Protocol shape `BridgeController` requires to construct."""

    def __init__(self, *, bot_id: str = "bot-999") -> None:
        self._bot_id = bot_id
        self.sent: list[tuple[str, str]] = []
        self._cb: Callable[[dict], Awaitable[None]] | None = None

    def on_message(self, cb: Callable[[dict], Awaitable[None]]) -> None:
        self._cb = cb

    async def send(self, channel_id: str, text: str) -> None:
        self.sent.append((channel_id, text))

    def me_id(self) -> str:
        return self._bot_id

    async def run(self) -> None:
        raise NotImplementedError("not exercised by forward-all tests")

    async def fetch_history(self, channel_id: str, limit: int) -> list[dict]:
        return []

    async def is_owner_in_guild(self, guild_id: str, owner_id: str) -> bool:
        """Not exercised here (tests set `owner_guild_only=False`, see
        `_cfg`) — present only to satisfy the `DiscordClient` Protocol shape
        `BridgeController` requires to construct. See
        test_owner_guild_gate.py for the gate's own dedicated suite."""
        raise NotImplementedError("not exercised by forward-all tests")


def _event(**kw) -> dict:
    base = dict(
        author_id="42", author="stranger", is_dm=False, mentioned=False,
        content="anyone up for a game", channel_id="c1", guild="g1",
        channel="general", msg_id="m1",
    )
    base.update(kw)
    return base


def _cfg(**kw) -> BridgeConfig:
    # name_aliases / always_wake_channels removed from BridgeConfig (Part A
    # A5, spec §3.6): dead config, since P1c moved L0/L1 wake admission
    # daemon-side into AttentionGate — the bridge never read either field.
    # channel_allowlist ALSO removed (Part B / B3, spec §4.3): it never
    # gated forwarding either — `_registered` now starts empty and is grown
    # entirely by register-on-first-forward, so a channel's FIRST forward in
    # a test (including "c1" below) always produces a ChannelRegister ahead
    # of its ChannelEvent; tests below account for that.
    # owner_guild_only=False here (Part B / B2): this suite is specifically
    # about forward-all behavior, which owner_guild_only's default-True gate
    # would otherwise partially undo — see test_owner_guild_gate.py for the
    # gate's own suite.
    base = dict(
        owner_id="owner-1", bot_id="bot-999",
        owner_guild_only=False,
    )
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


# ----- core change: non-self, no-L0-signal message is STILL forwarded -----


async def test_non_self_message_with_no_l0_signal_is_still_forwarded(tmp_path):
    """A stranger's unrelated public chatter — no mention, no DM, no name
    alias, not in always_wake_channels — used to be dropped by P1b's
    l0_wake gate (ambient-only). Under P1c Option A it must still be
    forwarded; L0/L1 admission is the daemon's AttentionGate's job now.

    channel_allowlist removal (Part B / B3): "c1" is no longer pre-
    registered, so this first message on it also produces a ChannelRegister
    ahead of the ChannelEvent — filter to the ChannelEvent for the
    forward-all assertion below."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.on_discord_message(_event())

    events = [m for m in sent_to_daemon if isinstance(m, ChannelEvent)]
    assert len(events) == 1
    event = events[0]
    assert event.channel_id == "c1"
    assert event.payload["author_is_owner"] is False
    assert event.payload["msg_id"] == "m1"
    assert event.payload["content"] == "anyone up for a game"


# ----- self-filter stays -----


async def test_self_message_is_logged_but_never_forwarded(tmp_path):
    """Self-filter MUST stay: her own messages never reach the daemon
    (regression here reintroduces the self-echo infinite loop P1b fixed) —
    but they still get ambient-logged for corpus completeness."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.on_discord_message(
        _event(author_id="bot-999", content="gura here", mentioned=True, msg_id="m3")
    )

    lines = _ambient_lines(tmp_path, "g1", "c1")
    assert lines[0]["msg_id"] == "m3"
    assert sent_to_daemon == []  # self-filter wins even though mentioned=True


# ----- ambient.append fires for ALL messages, including self -----


async def test_ambient_append_fires_for_all_messages_including_self(tmp_path):
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.on_discord_message(_event(msg_id="m1"))
    await controller.on_discord_message(_event(author_id="bot-999", msg_id="m2"))
    await controller.on_discord_message(_event(msg_id="m3", mentioned=True))

    lines = _ambient_lines(tmp_path, "g1", "c1")
    assert [line["msg_id"] for line in lines] == ["m1", "m2", "m3"]

    # only m1 and m3 (non-self) were forwarded — m2 (self) logged, not
    # forwarded. m1 (the first forward on "c1") also fires exactly one
    # ChannelRegister (channel_allowlist removal, Part B / B3) — m2's
    # self-filter returns before that check ever runs, and m3 finds "c1"
    # already registered, so still exactly one register overall.
    events = [m for m in sent_to_daemon if isinstance(m, ChannelEvent)]
    assert len(events) == 2
    assert [e.payload["msg_id"] for e in events] == ["m1", "m3"]

    registers = [m for m in sent_to_daemon if isinstance(m, ChannelRegister)]
    assert len(registers) == 1


# ----- dynamic ChannelRegister now keys off first FORWARD, not first wake -----


async def test_first_forward_from_unregistered_channel_registers_once(tmp_path):
    """A channel outside the static allowlist gets its external sink
    registered the first time any of its messages is FORWARDED — no L0
    signal required anymore, since forwarding itself no longer depends on
    L0."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    # c2 has never forwarded a message yet and carries NO L0 signal at all —
    # still forwards (Option A) and still triggers first-forward registration.
    await controller.on_discord_message(_event(channel_id="c2", msg_id="m1"))

    assert len(sent_to_daemon) == 2
    reg, ev = sent_to_daemon
    assert isinstance(reg, ChannelRegister)
    assert (reg.channel_id, reg.locus, reg.kind) == ("c2", "external", "discord")
    assert isinstance(ev, ChannelEvent)
    assert ev.channel_id == "c2"

    # second message from c2 → no duplicate ChannelRegister, just another forward.
    await controller.on_discord_message(_event(channel_id="c2", msg_id="m2"))

    registers = [m for m in sent_to_daemon if isinstance(m, ChannelRegister)]
    events = [m for m in sent_to_daemon if isinstance(m, ChannelEvent)]
    assert len(registers) == 1
    assert len(events) == 2


async def test_self_message_from_unregistered_channel_does_not_register(tmp_path):
    """A self-authored message in a not-yet-registered channel is logged but
    never forwarded, so it must NOT trigger the dynamic ChannelRegister
    either — registration is tied to forwarding, not to ambient logging."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.on_discord_message(
        _event(channel_id="c2", author_id="bot-999", msg_id="m1")
    )

    lines = _ambient_lines(tmp_path, "g1", "c2")
    assert lines[0]["msg_id"] == "m1"
    assert sent_to_daemon == []
