"""Bridge controller: message in -> ambient+ChannelEvent; AddressedText -> send.

Task 5's contract (brief §Task 5 Step 1), against a `FakeDiscordClient` (this
task authors it fresh — Task 4 shipped the `DiscordClient` Protocol but no
Fake, see task-4-report.md's "Concerns"), a recording `daemon_send`, and a
real `AmbientLog` writing under `tmp_path`:

  (a) a stranger's unrelated message -> ambient.append called, NO ChannelEvent.
  (b) a message mentioning her -> ambient.append AND a ChannelEvent (payload
      carries author_is_owner correctly derived from owner_id) sent to daemon.
  (c) an AddressedText(channel_id, text) from daemon -> discord.send(channel_id, text).
  (d) her OWN message (author_id==bot_id) -> ambient.append but NO ChannelEvent
      (self-filter, spec §3.3 C3).
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from dollos.discord_bridge.ambient_log import AmbientLog
from dollos.discord_bridge.controller import BridgeConfig, BridgeController
from dollos.ipc.messages import AddressedText, ChannelEvent


class FakeDiscordClient:
    """Test double for the `DiscordClient` Protocol (Task 4's client.py).

    Records every `send()` call, returns a fixed `me_id()`, and lets a test
    push a synthetic event into the registered `on_message` callback via
    `push()` — mirroring what `PycordClient`'s real `on_message` handler
    would invoke. Never touches py-cord or the network.
    """

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
        raise NotImplementedError("FakeDiscordClient.run() is not exercised by controller tests")

    async def push(self, event: dict) -> None:
        """Test helper: deliver `event` to the registered on_message callback."""
        assert self._cb is not None, "on_message callback was never registered"
        await self._cb(event)


def _event(**kw) -> dict:
    base = dict(
        author_id="42", author="stranger", is_dm=False, mentioned=False,
        content="anyone up for a game", channel_id="c1", guild="g1",
        channel="general", msg_id="m1",
    )
    base.update(kw)
    return base


def _cfg(**kw) -> BridgeConfig:
    base = dict(
        owner_id="owner-1", name_aliases=["gura", "古拉"],
        always_wake_channels=set(), bot_id="bot-999",
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


# ----- (a) stranger, unrelated content -----


async def test_stranger_unrelated_message_logs_but_does_not_wake(tmp_path):
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.on_discord_message(_event())

    lines = _ambient_lines(tmp_path, "g1", "c1")
    assert lines[0]["msg_id"] == "m1"
    assert sent_to_daemon == []


# ----- (b) message mentioning her -----


async def test_mention_logs_and_wakes_with_correct_author_is_owner(tmp_path):
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.on_discord_message(
        _event(mentioned=True, author_id="42", content="hey are you there")
    )

    lines = _ambient_lines(tmp_path, "g1", "c1")
    assert lines[0]["mentioned"] is True

    assert len(sent_to_daemon) == 1
    event = sent_to_daemon[0]
    assert isinstance(event, ChannelEvent)
    assert event.channel_id == "c1"
    assert event.payload["author_is_owner"] is False   # "42" != owner_id


async def test_mention_from_owner_marks_author_is_owner_true(tmp_path):
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.on_discord_message(
        _event(mentioned=True, author_id="owner-1", msg_id="m2")
    )

    assert len(sent_to_daemon) == 1
    assert sent_to_daemon[0].payload["author_is_owner"] is True


# ----- (c) AddressedText routes back to Discord -----


async def test_addressed_text_from_daemon_sends_to_discord(tmp_path):
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.on_daemon_message(AddressedText(channel_id="c1", text="hi there"))

    assert discord.sent == [("c1", "hi there")]


async def test_non_addressed_text_daemon_message_is_ignored(tmp_path):
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.on_daemon_message(object())

    assert discord.sent == []


# ----- (d) self-filter: bot's own message -----


async def test_own_message_logs_but_never_wakes(tmp_path):
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.on_discord_message(
        _event(author_id="bot-999", content="gura here", mentioned=True, msg_id="m3")
    )

    lines = _ambient_lines(tmp_path, "g1", "c1")
    assert lines[0]["msg_id"] == "m3"
    assert sent_to_daemon == []          # self-filter wins even though mentioned=True


# ----- end-to-end via FakeDiscordClient.push() (registered callback path) -----


async def test_push_through_registered_callback_drives_on_discord_message(tmp_path):
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)
    discord.on_message(controller.on_discord_message)

    await discord.push(_event(is_dm=True, guild=None, content="hi", msg_id="m4"))

    lines = _ambient_lines(tmp_path, "dm", "c1")   # no guild -> "dm" bucket
    assert lines[0]["msg_id"] == "m4"
    assert len(sent_to_daemon) == 1
    assert sent_to_daemon[0].payload["author_is_owner"] is False
