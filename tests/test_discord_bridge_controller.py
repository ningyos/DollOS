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
from datetime import UTC, datetime

from dollos.discord_bridge.ambient_log import AmbientLog
from dollos.discord_bridge.client import RateLimited
from dollos.discord_bridge.controller import BridgeConfig, BridgeController, _event_date
from dollos.ipc.messages import AddressedText, ChannelEvent, ChannelRegister


class FakeDiscordClient:
    """Test double for the `DiscordClient` Protocol (Task 4's client.py).

    Records every `send()` call, returns a fixed `me_id()`, and lets a test
    push a synthetic event into the registered `on_message` callback via
    `push()` — mirroring what `PycordClient`'s real `on_message` handler
    would invoke. Never touches py-cord or the network.

    `raise_rate_limited_once()` arms a one-shot `RateLimited` on the NEXT
    `send()` call only (Task 6: 429 retry) — the call after that succeeds
    normally, so tests can assert the controller retried exactly once.
    """

    def __init__(self, *, bot_id: str = "bot-999") -> None:
        self._bot_id = bot_id
        self.sent: list[tuple[str, str]] = []
        self.send_attempts = 0
        self._cb: Callable[[dict], Awaitable[None]] | None = None
        self._rate_limit_once_after: float | None = None
        self._history: dict[str, list[dict]] = {}

    def on_message(self, cb: Callable[[dict], Awaitable[None]]) -> None:
        self._cb = cb

    async def send(self, channel_id: str, text: str) -> None:
        self.send_attempts += 1
        if self._rate_limit_once_after is not None:
            retry_after = self._rate_limit_once_after
            self._rate_limit_once_after = None
            raise RateLimited(retry_after)
        self.sent.append((channel_id, text))

    def raise_rate_limited_once(self, retry_after: float) -> None:
        self._rate_limit_once_after = retry_after

    def me_id(self) -> str:
        return self._bot_id

    async def run(self) -> None:
        raise NotImplementedError("FakeDiscordClient.run() is not exercised by controller tests")

    async def push(self, event: dict) -> None:
        """Test helper: deliver `event` to the registered on_message callback."""
        assert self._cb is not None, "on_message callback was never registered"
        await self._cb(event)

    async def fetch_history(self, channel_id: str, limit: int) -> list[dict]:
        """Test double for `DiscordClient.fetch_history` (Task 7): returns
        whatever `set_history()` staged for `channel_id`, ignoring `limit`
        (tests stage exactly the events they want returned)."""
        return list(self._history.get(channel_id, []))

    def set_history(self, channel_id: str, events: list[dict]) -> None:
        """Test helper: stage the events `fetch_history(channel_id, ...)` returns."""
        self._history[channel_id] = events


def _event(**kw) -> dict:
    base = dict(
        author_id="42", author="stranger", is_dm=False, mentioned=False,
        content="anyone up for a game", channel_id="c1", guild="g1",
        channel="general", msg_id="m1",
    )
    base.update(kw)
    return base


def _cfg(**kw) -> BridgeConfig:
    # channel_allowlist defaults to ["c1"] — the primary channel these tests
    # exercise — so it counts as pre-registered (`__main__.py` registers the
    # allowlist on connect) and a wake from it fires NO dynamic
    # ChannelRegister. Tests exercising the dynamic register-on-first-wake
    # path (P1b review) use a channel_id OUTSIDE this allowlist.
    base = dict(
        owner_id="owner-1", name_aliases=["gura", "古拉"],
        always_wake_channels=set(), bot_id="bot-999",
        channel_allowlist=["c1"],
    )
    base.update(kw)
    return BridgeConfig(**base)


def _make(tmp_path, *, sleep=None, **cfg_kw):
    discord = FakeDiscordClient(bot_id=cfg_kw.pop("bot_id", "bot-999"))
    sent_to_daemon: list[object] = []

    async def daemon_send(msg: object) -> None:
        sent_to_daemon.append(msg)

    ambient = AmbientLog(tmp_path, retention_days=30)
    cfg = _cfg(bot_id=discord.me_id(), **cfg_kw)
    extra = {} if sleep is None else {"sleep": sleep}
    controller = BridgeController(discord, daemon_send, ambient, cfg, **extra)
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


# ----- Task 6 (a): backfill dedup — carry I-2 idempotency -----


async def test_backfill_skips_already_seen_msg_ids_entirely(tmp_path):
    """A msg_id already logged (live, before reconnect) must NOT be re-logged
    and must NOT re-fire a ChannelEvent when it comes back through backfill —
    even if it's wake-worthy. Only genuinely new events append + (if
    wake-worthy) wake."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    # Live traffic before the reconnect gap: m1 ambient-only, m2 wake-worthy.
    await controller.on_discord_message(_event(msg_id="m1"))
    await controller.on_discord_message(
        _event(msg_id="m2", mentioned=True, content="hey are you there")
    )
    assert len(sent_to_daemon) == 1
    assert sent_to_daemon[0].payload["msg_id"] == "m2"

    # Reconnect backfill replays m1 (dup), m2 (dup, wake-worthy) and m3 (new,
    # wake-worthy) — as Discord channel history would after a gap.
    await controller.backfill(
        "g1",
        "c1",
        [
            _event(msg_id="m1"),
            _event(msg_id="m2", mentioned=True, content="hey are you there"),
            _event(msg_id="m3", mentioned=True, content="hey are you there"),
        ],
    )

    lines = _ambient_lines(tmp_path, "g1", "c1")
    assert [line["msg_id"] for line in lines] == ["m1", "m2", "m3"]  # no dup lines

    # m1/m2 replays fired NEITHER a dup ambient line NOR a dup ChannelEvent —
    # only the brand-new m3 wakes.
    assert len(sent_to_daemon) == 2
    assert [e.payload["msg_id"] for e in sent_to_daemon] == ["m2", "m3"]


async def test_backfill_ambient_only_events_never_wake(tmp_path):
    """A backfilled event that is new but NOT wake-worthy still logs, but
    never fires a ChannelEvent (same L0 rule as live traffic)."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.backfill("g1", "c1", [_event(msg_id="m5")])

    lines = _ambient_lines(tmp_path, "g1", "c1")
    assert lines[0]["msg_id"] == "m5"
    assert sent_to_daemon == []


# ----- Task 6 (b): 429-aware send retries once -----


async def test_send_retries_once_after_rate_limited_then_succeeds(tmp_path):
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    controller, discord, sent_to_daemon, ambient = _make(tmp_path, sleep=fake_sleep)
    discord.raise_rate_limited_once(retry_after=1.5)

    await controller.on_daemon_message(AddressedText(channel_id="c1", text="hi there"))

    assert slept == [1.5]
    assert discord.send_attempts == 2
    assert discord.sent == [("c1", "hi there")]


# ----- Task 7 (a): _event_date true-timestamp LANDMINE (review) -----


def test_event_date_uses_true_timestamp_not_wallclock():
    """LANDMINE (review): a backfilled event dated near a UTC boundary must
    bucket by its TRUE post date, not today's wall-clock — else the same
    msg_id lands in two date files across midnight and re-wakes on replay."""
    ts = 1751500740.0  # 2025-07-02 23:59:00 UTC — deliberately not "today"
    ev = {"msg_id": "m1", "content": "x", "ts": ts}

    expected = datetime.fromtimestamp(ts, UTC).date().isoformat()

    assert _event_date(ev) == expected
    assert _event_date(ev) != datetime.now(UTC).date().isoformat()


def test_event_date_falls_back_to_wallclock_when_no_ts():
    """An event with no `ts` at all (no live Discord event should lack one,
    but the fallback is kept as best-effort) still buckets by wall-clock,
    same as before Task 7."""
    ev = {"msg_id": "m1", "content": "x"}

    assert _event_date(ev) == datetime.now(UTC).date().isoformat()


# ----- Task 7 (b): reconnect_backfill fetches history + dedups -----


async def test_reconnect_backfill_feeds_history_and_dedups(tmp_path):
    """`reconnect_backfill` fetches each channel's recent history via the
    injected `fetch` callable and replays it through the same capture+wake
    path as `backfill()` — a msg_id already in the ambient log (pre-seeded,
    as if logged before the reconnect gap) fires no dup ChannelEvent; only
    the genuinely new, wake-worthy event does."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    seen_event = _event(msg_id="m1", ts=1751500000.0)
    new_event = _event(
        msg_id="m2", ts=1751500100.0, mentioned=True, content="hey are you there"
    )

    # Pre-seed ambient with m1, as if it was logged live before the reconnect gap.
    ambient.append("g1", "c1", {**seen_event, "date": _event_date(seen_event)})

    discord.set_history("c1", [seen_event, new_event])

    await controller.reconnect_backfill(discord.fetch_history, ["c1"])

    lines = _ambient_lines(tmp_path, "g1", "c1")
    assert [line["msg_id"] for line in lines] == ["m1", "m2"]  # no dup line for m1

    assert len(sent_to_daemon) == 1
    assert sent_to_daemon[0].payload["msg_id"] == "m2"


# ----- P1b review: dynamic ChannelRegister on first wake -----


async def test_first_wake_from_unregistered_channel_registers_once(tmp_path):
    """A wake from a channel NOT in the static allowlist must dynamically
    register an external sink (ChannelRegister) BEFORE the ChannelEvent, so
    the daemon can route her reply back — and exactly once per channel per
    session (idempotent on a second wake from the same channel)."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)  # allowlist=["c1"]

    # c2 is NOT allowlisted → first wake must register it.
    await controller.on_discord_message(
        _event(channel_id="c2", mentioned=True, content="hey are you there", msg_id="m1")
    )

    assert len(sent_to_daemon) == 2
    reg, ev = sent_to_daemon
    assert isinstance(reg, ChannelRegister)
    assert (reg.channel_id, reg.locus, reg.kind) == ("c2", "external", "discord")
    assert isinstance(ev, ChannelEvent)
    assert ev.channel_id == "c2"

    # Second wake from c2 → NO duplicate ChannelRegister (idempotent), just
    # another ChannelEvent.
    await controller.on_discord_message(
        _event(channel_id="c2", mentioned=True, content="still there?", msg_id="m2")
    )

    registers = [m for m in sent_to_daemon if isinstance(m, ChannelRegister)]
    events = [m for m in sent_to_daemon if isinstance(m, ChannelEvent)]
    assert len(registers) == 1
    assert len(events) == 2


async def test_allowlisted_channel_wake_does_not_re_register(tmp_path):
    """A wake from an allowlisted channel (already registered by __main__ on
    connect, seeded into the controller) must NOT re-send ChannelRegister —
    only the ChannelEvent."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)  # allowlist=["c1"]

    await controller.on_discord_message(
        _event(mentioned=True, content="hey are you there")  # channel_id="c1"
    )

    assert [type(m) for m in sent_to_daemon] == [ChannelEvent]


async def test_dm_wake_registers_so_reply_can_route(tmp_path):
    """A DM always wakes (l0), and its channel id is never in the static
    allowlist, so it must dynamically register — otherwise her DM reply has
    no external sink and mis-routes internally (P1b review: half-breaks the
    owner-DM path)."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.on_discord_message(
        _event(is_dm=True, guild=None, channel_id="dm-77", content="hi", msg_id="m1")
    )

    assert isinstance(sent_to_daemon[0], ChannelRegister)
    assert (sent_to_daemon[0].channel_id, sent_to_daemon[0].locus,
            sent_to_daemon[0].kind) == ("dm-77", "external", "discord")
    assert isinstance(sent_to_daemon[1], ChannelEvent)
    assert sent_to_daemon[1].channel_id == "dm-77"
