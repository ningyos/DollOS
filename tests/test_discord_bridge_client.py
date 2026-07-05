"""PycordClient: real py-cord 429 translation (P1b Task 7).

`py-cord` is a hard dependency (`pyproject.toml`: "py-cord>=2.6"), so this
test exercises the REAL `discord.HTTPException` type — never a live Discord
connection, just enough of py-cord's object shape (a minimal response stub)
to drive `PycordClient.send`'s translation logic. `client.py` stays
importable without py-cord actually connecting to anything (see the module
docstring); these tests only construct plain objects and call `send()`
directly against a stubbed `_bot`, never `PycordClient.run()`.
"""
from __future__ import annotations

from datetime import UTC, datetime

import discord
import pytest

from dollos.discord_bridge.client import PycordClient, RateLimited, _to_event


class _FakeResponse:
    """Minimal stand-in for `aiohttp.ClientResponse` — just enough for
    `discord.HTTPException.__init__`'s formatting (`.status`, `.reason`) and
    for a 429's `Retry-After` header."""

    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.reason = "stub reason"
        self.headers = headers or {}


class _FakeChannel:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def send(self, text: str) -> None:
        raise self._exc


class _FakeBot:
    def __init__(self, channel: _FakeChannel) -> None:
        self._channel = channel

    def get_channel(self, channel_id: int):
        return self._channel


def _connected_client(exc: Exception) -> PycordClient:
    client = PycordClient(token="fake-token")
    client._bot = _FakeBot(_FakeChannel(exc))
    return client


async def test_send_translates_real_pycord_429_to_rate_limited():
    exc = discord.HTTPException(
        _FakeResponse(429, {"Retry-After": "2.5"}),
        {"message": "You are being rate limited.", "retry_after": 2.5, "global": False},
    )
    client = _connected_client(exc)

    with pytest.raises(RateLimited) as exc_info:
        await client.send("123", "hi")

    assert exc_info.value.retry_after == 2.5


async def test_send_reraises_non_429_http_exception():
    exc = discord.HTTPException(_FakeResponse(500), {"message": "server error"})
    client = _connected_client(exc)

    with pytest.raises(discord.HTTPException):
        await client.send("123", "hi")


# ----- _to_event: reply_to_bot L0 signal (P1c whole-branch review Important #1) -----
#
# Spec §3.4 lists L0 = dm/mention/name/reply-to-her/always_wake, but the
# translator never populated `reply_to_bot` — `AttentionGate._l0_signal`'s
# `l0_reply` branch was dead in production even though a unit test injected
# the field manually. These tests drive `_to_event` with minimal duck-typed
# fakes (no real py-cord objects needed — `_to_event` only reads attributes)
# to prove the translator itself sets `reply_to_bot` correctly from
# `message.reference.resolved.author.id`.


class _ReplyUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _ReplyChannel:
    def __init__(self, channel_id: int, name: str = "general") -> None:
        self.id = channel_id
        self.name = name


class _ReplyResolvedMessage:
    """Stand-in for a resolved `discord.Message` — only `.author` matters."""

    def __init__(self, author_id: int) -> None:
        self.author = _ReplyUser(author_id)


class _ReplyReference:
    """Stand-in for `discord.MessageReference` — only `.resolved` matters."""

    def __init__(self, resolved: object | None = None) -> None:
        self.resolved = resolved


class _ReplyMessage:
    def __init__(
        self,
        *,
        author_id: int = 42,
        content: str = "hi",
        channel_id: int = 1,
        guild: object | None = None,
        mentions: list | None = None,
        reference: _ReplyReference | None = None,
        msg_id: int = 1,
    ) -> None:
        self.author = _ReplyUser(author_id)
        self.content = content
        self.channel = _ReplyChannel(channel_id)
        self.guild = guild
        self.mentions = mentions or []
        self.reference = reference
        self.id = msg_id
        self.created_at = datetime.now(UTC)


class _ReplyBot:
    def __init__(self, user_id: int | None) -> None:
        self.user = _ReplyUser(user_id) if user_id is not None else None


def test_to_event_reply_to_bot_true_when_replying_to_bot_ping_off():
    """Discord's 'reply' button with ping OFF still counts as reply_to_bot
    even though `mentioned` stays False — this is exactly the L0 signal
    `l0_reply` needs and that a ping-off reply-to-her, no-active-session
    message depends on to be admitted at all."""
    bot = _ReplyBot(user_id=999)
    message = _ReplyMessage(
        author_id=1,
        mentions=[],  # ping off -> mentioned computes to False
        reference=_ReplyReference(resolved=_ReplyResolvedMessage(author_id=999)),
    )

    event = _to_event(message, bot)

    assert event["mentioned"] is False
    assert event["reply_to_bot"] is True


def test_to_event_reply_to_bot_false_when_replying_to_non_bot():
    bot = _ReplyBot(user_id=999)
    message = _ReplyMessage(
        reference=_ReplyReference(resolved=_ReplyResolvedMessage(author_id=111)),
    )

    event = _to_event(message, bot)

    assert event["reply_to_bot"] is False


def test_to_event_reply_to_bot_false_when_not_a_reply():
    bot = _ReplyBot(user_id=999)
    message = _ReplyMessage(reference=None)

    event = _to_event(message, bot)

    assert event["reply_to_bot"] is False


def test_to_event_reply_to_bot_false_when_reference_unresolved():
    """A reply whose referenced message py-cord hasn't resolved (`resolved`
    is None — not in the internal cache) is treated as not-a-reply-to-bot
    rather than raising or triggering an extra fetch — a documented
    limitation (see `_to_event`'s docstring), not a crash."""
    bot = _ReplyBot(user_id=999)
    message = _ReplyMessage(reference=_ReplyReference(resolved=None))

    event = _to_event(message, bot)

    assert event["reply_to_bot"] is False
