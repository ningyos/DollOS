"""Bridge controller: message in -> ambient+ChannelEvent; AddressedText -> send.

Task 5's contract (brief §Task 5 Step 1), against a `FakeDiscordClient` (this
task authors it fresh — Task 4 shipped the `DiscordClient` Protocol but no
Fake, see task-4-report.md's "Concerns"), a recording `daemon_send`, and a
real `AmbientLog` writing under `tmp_path`:

  (a) a stranger's unrelated message -> ambient.append called AND a
      ChannelEvent forwarded (P1c Task 3, Option A: forward-all — see below).
  (b) a message mentioning her -> ambient.append AND a ChannelEvent (payload
      carries author_is_owner correctly derived from owner_id) sent to daemon.
  (c) an AddressedText(channel_id, text) from daemon -> discord.send(channel_id, text).
  (d) her OWN message (author_id==bot_id) -> ambient.append but NO ChannelEvent
      (self-filter, spec §3.3 C3).

P1c Task 3 update (Option A, spec §3.4 "全量訊息都送 daemon"): the bridge no
longer runs a local `l0_wake` gate before forwarding — L0/L1/L2 admission
moved daemon-side into `AttentionGate`. So (a) above changed from "no
ChannelEvent" (P1b) to "still forwarded" (P1c): every non-self message
forwards now, wake-worthy or not. The tests below that asserted the OLD
ambient-only-for-unwoken-messages behavior are updated accordingly; the
self-filter (d) is unchanged — it's the only gate left on the bridge side.
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

    `raise_http_exception_once()` arms a one-shot non-retryable
    `discord.HTTPException` (e.g. Discord's `400 code 50006: Cannot send an
    empty message`) on the NEXT `send()` call only — the empty-speech-chunk
    regression: `_send_with_retry` must drop this, not let it propagate and
    tear down the whole bridge connection.
    """

    def __init__(self, *, bot_id: str = "bot-999") -> None:
        self._bot_id = bot_id
        self.sent: list[tuple[str, str]] = []
        self.send_attempts = 0
        self._cb: Callable[[dict], Awaitable[None]] | None = None
        self._rate_limit_once_after: float | None = None
        self._http_exc_status_once: int | None = None
        self._history: dict[str, list[dict]] = {}
        # owner_guild_only gate (Part B / B2): configurable per test via
        # `set_owner_guild`/`raise_on_guild`. These tests exercise forward-
        # all behavior (unrelated to the gate) and set `owner_guild_only=
        # False` in `_cfg`, so `is_owner_in_guild` is not called by default —
        # it's here so the Fake still satisfies the `DiscordClient` Protocol
        # shape `BridgeController` requires to construct.
        self._owner_guilds: set[str] = set()
        self._raise_for_guilds: set[str] = set()
        self.is_owner_in_guild_calls: list[str] = []

    def on_message(self, cb: Callable[[dict], Awaitable[None]]) -> None:
        self._cb = cb

    async def send(self, channel_id: str, text: str) -> None:
        self.send_attempts += 1
        if self._rate_limit_once_after is not None:
            retry_after = self._rate_limit_once_after
            self._rate_limit_once_after = None
            raise RateLimited(retry_after)
        if self._http_exc_status_once is not None:
            status = self._http_exc_status_once
            self._http_exc_status_once = None
            import discord

            class _FakeResponse:
                reason = "Bad Request"

            resp = _FakeResponse()
            resp.status = status
            raise discord.HTTPException(resp, {"code": 50006, "message": "Cannot send an empty message"})
        self.sent.append((channel_id, text))

    def raise_rate_limited_once(self, retry_after: float) -> None:
        self._rate_limit_once_after = retry_after

    def raise_http_exception_once(self, status: int = 400) -> None:
        self._http_exc_status_once = status

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

    def set_owner_guild(self, guild_id: str, is_member: bool) -> None:
        """Test helper: configure whether the owner is a member of `guild_id`."""
        if is_member:
            self._owner_guilds.add(guild_id)
        else:
            self._owner_guilds.discard(guild_id)

    def raise_on_guild(self, guild_id: str) -> None:
        """Test helper: `is_owner_in_guild(guild_id, ...)` raises next call."""
        self._raise_for_guilds.add(guild_id)

    async def is_owner_in_guild(self, guild_id: str, owner_id: str) -> bool:
        self.is_owner_in_guild_calls.append(guild_id)
        if guild_id in self._raise_for_guilds:
            raise RuntimeError("simulated is_owner_in_guild failure")
        return guild_id in self._owner_guilds


def _event(**kw) -> dict:
    base = dict(
        author_id="42", author="stranger", is_dm=False, mentioned=False,
        content="anyone up for a game", channel_id="c1", guild="g1",
        channel="general", msg_id="m1",
    )
    base.update(kw)
    return base


def _cfg(**kw) -> BridgeConfig:
    # channel_allowlist removed from BridgeConfig (Part B / B3, spec §4.3):
    # it never gated forwarding and its pre-register/backfill-scope uses are
    # both superseded (register-on-first-forward; optional
    # backfill_channels). `_registered` now starts truly empty, so "c1" — the
    # primary channel most tests below exercise — fires its OWN dynamic
    # ChannelRegister the first time any test forwards through it, exactly
    # like any other channel. Tests below filter to ChannelEvent where they
    # only care about forwarded payloads, and assert the register explicitly
    # where that is the point of the test.
    # name_aliases / always_wake_channels removed from BridgeConfig (Part A
    # A5, spec §3.6): dead config, since P1c moved L0/L1 wake admission
    # daemon-side into AttentionGate — the bridge never read either field.
    # owner_guild_only defaults to False here (Part B / B2): these tests
    # exercise forward-all / register / backfill behavior that predates and
    # is orthogonal to the owner_guild_only gate — see test_owner_guild_gate.py
    # for the gate's own dedicated suite. BridgeConfig's own default is True
    # (D7, safe-by-default); tests opt back out explicitly.
    base = dict(
        owner_id="owner-1", bot_id="bot-999",
        owner_guild_only=False,
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


async def test_stranger_unrelated_message_logs_and_is_forwarded(tmp_path):
    """P1c Task 3 (Option A): this used to assert NO ChannelEvent (P1b's
    local l0_wake gate dropped ambient-only chatter). Now the bridge
    forwards every non-self message regardless of wake-worthiness — the
    daemon's AttentionGate decides admission (see test_discord_forward_all.py
    for the dedicated forward-all test suite).

    channel_allowlist removal (Part B / B3): "c1" is not pre-registered
    anymore, so this first message on it also fires a ChannelRegister —
    filter to the ChannelEvent for the payload assertion."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.on_discord_message(_event())

    lines = _ambient_lines(tmp_path, "g1", "c1")
    assert lines[0]["msg_id"] == "m1"
    events = [m for m in sent_to_daemon if isinstance(m, ChannelEvent)]
    assert len(events) == 1
    assert events[0].payload["msg_id"] == "m1"


# ----- (b) message mentioning her -----


async def test_mention_logs_and_forwards_with_correct_author_is_owner(tmp_path):
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.on_discord_message(
        _event(mentioned=True, author_id="42", content="hey are you there")
    )

    lines = _ambient_lines(tmp_path, "g1", "c1")
    assert lines[0]["mentioned"] is True

    events = [m for m in sent_to_daemon if isinstance(m, ChannelEvent)]
    assert len(events) == 1
    event = events[0]
    assert event.channel_id == "c1"
    assert event.payload["author_is_owner"] is False   # "42" != owner_id


async def test_mention_from_owner_marks_author_is_owner_true(tmp_path):
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.on_discord_message(
        _event(mentioned=True, author_id="owner-1", msg_id="m2")
    )

    events = [m for m in sent_to_daemon if isinstance(m, ChannelEvent)]
    assert len(events) == 1
    assert events[0].payload["author_is_owner"] is True


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


async def test_own_message_logs_but_is_never_forwarded(tmp_path):
    """Self-filter (spec §3.3 C3) is the ONE gate P1c Task 3 keeps on the
    bridge side — unaffected by the forward-all change above (a)."""
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
    events = [m for m in sent_to_daemon if isinstance(m, ChannelEvent)]
    assert len(events) == 1
    assert events[0].payload["author_is_owner"] is False


# ----- Task 6 (a): backfill dedup — carry I-2 idempotency -----


async def test_backfill_skips_already_seen_msg_ids_entirely(tmp_path):
    """A msg_id already logged (live, before reconnect) must NOT be re-logged
    and must NOT re-forwarded when it comes back through backfill.

    P1c Task 3 (Option A) update: under forward-all every fresh non-self
    event forwards now (m1 included — it used to be ambient-only under
    P1b's l0_wake gate). What this test still guards is the I-2 idempotency
    invariant: a msg_id already forwarded/logged once must never be
    re-logged or re-forwarded on replay — only the brand-new m3 forwards
    out of the backfill batch.

    channel_allowlist removal (Part B / B3): m1 (the first-ever forward on
    "c1" in this fresh controller) also fires exactly one ChannelRegister —
    filtered out of the ChannelEvent-payload assertions below, since it is
    not what this test is guarding."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    # Live traffic before the reconnect gap: both m1 and m2 forward now
    # (forward-all — no local wake gate left to distinguish them).
    await controller.on_discord_message(_event(msg_id="m1"))
    await controller.on_discord_message(
        _event(msg_id="m2", mentioned=True, content="hey are you there")
    )
    events = [m for m in sent_to_daemon if isinstance(m, ChannelEvent)]
    assert len(events) == 2
    assert [e.payload["msg_id"] for e in events] == ["m1", "m2"]

    # Reconnect backfill replays m1 (dup), m2 (dup) and m3 (new) — as
    # Discord channel history would after a gap.
    await controller.backfill(
        "g1",
        "c1",
        [
            _event(msg_id="m1"),
            _event(msg_id="m2", mentioned=True, content="hey are you there"),
            _event(msg_id="m3", content="brand new message"),
        ],
    )

    lines = _ambient_lines(tmp_path, "g1", "c1")
    assert [line["msg_id"] for line in lines] == ["m1", "m2", "m3"]  # no dup lines

    # m1/m2 replays fired NEITHER a dup ambient line NOR a dup ChannelEvent —
    # only the brand-new m3 forwards. Exactly one ChannelRegister total
    # (fired once, on m1's first forward — never re-sent).
    events = [m for m in sent_to_daemon if isinstance(m, ChannelEvent)]
    assert len(events) == 3
    assert [e.payload["msg_id"] for e in events] == ["m1", "m2", "m3"]
    registers = [m for m in sent_to_daemon if isinstance(m, ChannelRegister)]
    assert len(registers) == 1


async def test_backfill_new_non_self_events_are_forwarded(tmp_path):
    """P1c Task 3 (Option A): a backfilled event that is new (not a msg_id
    dup) and has NO L0 signal at all (no mention/dm/name/always-wake) still
    forwards — the bridge no longer decides wake locally; only a self-
    authored event is excluded. This replaces the old P1b
    `test_backfill_ambient_only_events_never_wake`, which asserted the
    opposite under the now-removed l0_wake gate.

    channel_allowlist removal (Part B / B3): this is "c1"'s first-ever
    forward in this fresh controller, so it also fires a ChannelRegister —
    filter to the ChannelEvent for the payload assertion."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.backfill("g1", "c1", [_event(msg_id="m5")])

    lines = _ambient_lines(tmp_path, "g1", "c1")
    assert lines[0]["msg_id"] == "m5"
    events = [m for m in sent_to_daemon if isinstance(m, ChannelEvent)]
    assert len(events) == 1
    assert events[0].payload["msg_id"] == "m5"


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


# ----- empty-speech-chunk regression: one bad send must not tear down the
# whole bridge connection -----


async def test_send_http_exception_is_dropped_not_raised(tmp_path, caplog):
    """Fix B (defense in depth): a non-retryable `discord.HTTPException`
    (e.g. Discord's `400 code 50006: Cannot send an empty message`, which is
    exactly what an owner DM hit when Fix A's bug sent a whitespace-only
    AddressedText first) must be caught and dropped inside
    `_send_with_retry` — NOT propagate out of `on_daemon_message`. Before the
    fix this raised straight out of `_send_with_retry`, which (in the real
    `__main__.py` `_connect_and_run`) tore down the whole daemon-WS +
    Discord-gateway connection over ONE bad send."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)
    discord.raise_http_exception_once(status=400)

    with caplog.at_level("WARNING"):
        await controller.on_daemon_message(AddressedText(channel_id="c1", text=""))

    assert discord.send_attempts == 1  # no retry loop for a non-429 failure
    assert discord.sent == []  # the bad message was dropped, not delivered
    assert any(
        record.levelname == "WARNING" for record in caplog.records
    ), "expected a warning to be logged when the send is dropped"


async def test_rate_limited_retry_still_works_alongside_http_exception_handling(tmp_path):
    """Regression guard for Fix B: adding the `discord.HTTPException` catch
    must not disturb the existing 429 retry-once behavior."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    controller, discord, sent_to_daemon, ambient = _make(tmp_path, sleep=fake_sleep)
    discord.raise_rate_limited_once(retry_after=2.0)

    await controller.on_daemon_message(AddressedText(channel_id="c1", text="hi there"))

    assert slept == [2.0]
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
    injected `fetch` callable and replays it through the same capture+forward
    path as `backfill()` — a msg_id already in the ambient log (pre-seeded,
    as if logged before the reconnect gap) fires no dup ChannelEvent; only
    the genuinely new event does. (Unaffected by P1c Task 3's forward-all
    change — `new_event`'s `mentioned=True` is incidental here, not what
    makes it forward; m1 is skipped purely on the I-2 dedup path, not on
    any wake-worthiness distinction.)

    channel_allowlist removal (Part B / B3): m1 is skipped BEFORE the
    register-on-first-forward check even runs (ambient dedup returns first),
    so m2 is "c1"'s first forward in this controller and also fires a
    ChannelRegister — filtered out of the ChannelEvent-payload assertion."""
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

    events = [m for m in sent_to_daemon if isinstance(m, ChannelEvent)]
    assert len(events) == 1
    assert events[0].payload["msg_id"] == "m2"


# ----- P1b review, updated by P1c Task 3: dynamic ChannelRegister now keys
# off first FORWARD instead of first WAKE (there's no local wake concept
# left — see test_discord_forward_all.py for the no-L0-signal variant of
# this same registration test). These still use mention/DM events because
# that's what P1b originally exercised, but the registration now fires
# purely because the event is non-self and forwarded, not because it
# happens to carry an L0 signal. channel_allowlist removal (Part B / B3)
# means there is no longer any "already pre-registered" channel at all —
# every channel, including "c1", registers dynamically on its own first
# forward; see test_registered_starts_empty_and_grows_via_register_on_
# first_forward below for that invariant made explicit. -----


async def test_first_forward_from_unregistered_channel_registers_once(tmp_path):
    """A forward from a channel that hasn't forwarded before must
    dynamically register an external sink (ChannelRegister) BEFORE the
    ChannelEvent, so the daemon can route her reply back — and exactly once
    per channel per session (idempotent on a second forward from the same
    channel)."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    # c2 has never forwarded a message yet → first forward must register it.
    await controller.on_discord_message(
        _event(channel_id="c2", mentioned=True, content="hey are you there", msg_id="m1")
    )

    assert len(sent_to_daemon) == 2
    reg, ev = sent_to_daemon
    assert isinstance(reg, ChannelRegister)
    assert (reg.channel_id, reg.locus, reg.kind) == ("c2", "external", "discord")
    assert isinstance(ev, ChannelEvent)
    assert ev.channel_id == "c2"

    # Second forward from c2 → NO duplicate ChannelRegister (idempotent),
    # just another ChannelEvent.
    await controller.on_discord_message(
        _event(channel_id="c2", mentioned=True, content="still there?", msg_id="m2")
    )

    registers = [m for m in sent_to_daemon if isinstance(m, ChannelRegister)]
    events = [m for m in sent_to_daemon if isinstance(m, ChannelEvent)]
    assert len(registers) == 1
    assert len(events) == 2


async def test_registered_starts_empty_and_grows_via_register_on_first_forward(
    tmp_path,
):
    """channel_allowlist (Part B / B3) used to seed `_registered` at
    construction; the removal's load-bearing invariant is that `_registered`
    now starts truly EMPTY and register-on-first-forward alone grows it —
    made explicit here rather than only inferred from the other tests in
    this module that incidentally exercise it."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    assert controller._registered == set()

    await controller.on_discord_message(_event())  # channel_id="c1"

    assert controller._registered == {"c1"}
    assert [type(m) for m in sent_to_daemon] == [ChannelRegister, ChannelEvent]

    # A second forward from the same channel does not re-register.
    await controller.on_discord_message(_event(msg_id="m2"))

    assert controller._registered == {"c1"}
    assert [type(m) for m in sent_to_daemon] == [
        ChannelRegister, ChannelEvent, ChannelEvent,
    ]


async def test_dm_forward_registers_so_reply_can_route(tmp_path):
    """A DM always forwards (non-self), and its channel id is never
    pre-registered, so it must dynamically register — otherwise her DM
    reply has no external sink and mis-routes internally (P1b review:
    half-breaks the owner-DM path)."""
    controller, discord, sent_to_daemon, ambient = _make(tmp_path)

    await controller.on_discord_message(
        _event(is_dm=True, guild=None, channel_id="dm-77", content="hi", msg_id="m1")
    )

    assert isinstance(sent_to_daemon[0], ChannelRegister)
    assert (sent_to_daemon[0].channel_id, sent_to_daemon[0].locus,
            sent_to_daemon[0].kind) == ("dm-77", "external", "discord")
    assert isinstance(sent_to_daemon[1], ChannelEvent)
    assert sent_to_daemon[1].channel_id == "dm-77"
