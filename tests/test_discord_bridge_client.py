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


# ----- is_owner_in_guild / owner_guild_channels: owner_guild_only
# detection primitive (Part B / B1). ALL failure modes fail-closed to
# False/[] — no gate wiring yet (B2), this only tests the primitive
# against a fake bot/guild, never real py-cord I/O. -----


class _FakeMember:
    def __init__(self, member_id: int) -> None:
        self.id = member_id


class _FakeGuildForOwnerCheck:
    """Stand-in for `discord.Guild`: `fetch_member` is configured per test
    to succeed, raise `discord.NotFound`, or raise an arbitrary transient
    exception. `text_channels` backs `owner_guild_channels`."""

    def __init__(
        self,
        guild_id: int,
        *,
        member_result: object = None,
        fetch_exc: Exception | None = None,
        text_channel_ids: list[int] | None = None,
    ) -> None:
        self.id = guild_id
        self._member_result = member_result
        self._fetch_exc = fetch_exc
        self.text_channels = [
            _FakeChannelStub(cid) for cid in (text_channel_ids or [])
        ]
        self.fetch_member_calls: list[int] = []

    async def fetch_member(self, member_id: int):
        self.fetch_member_calls.append(member_id)
        if self._fetch_exc is not None:
            raise self._fetch_exc
        return self._member_result if self._member_result is not None else _FakeMember(member_id)


class _FakeChannelStub:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id


class _FakeBotForGuilds:
    """Stand-in for `discord.Bot`: `get_guild` looks up by id (None if
    absent from cache, mirroring an unpopulated/unknown guild); `guilds`
    backs `owner_guild_channels`'s enumeration."""

    def __init__(self, guilds: list[_FakeGuildForOwnerCheck]) -> None:
        self._guilds_by_id = {g.id: g for g in guilds}
        self.guilds = guilds

    def get_guild(self, guild_id: int):
        return self._guilds_by_id.get(guild_id)


def _connected_client_for_guilds(guilds: list[_FakeGuildForOwnerCheck]) -> PycordClient:
    client = PycordClient(token="fake-token")
    client._bot = _FakeBotForGuilds(guilds)
    return client


async def test_is_owner_in_guild_true_when_member():
    guild = _FakeGuildForOwnerCheck(guild_id=1)
    client = _connected_client_for_guilds([guild])

    assert await client.is_owner_in_guild("1", "42") is True
    assert guild.fetch_member_calls == [42]


async def test_is_owner_in_guild_false_when_not_found():
    exc = discord.NotFound(_FakeResponse(404), {"message": "Unknown Member"})
    guild = _FakeGuildForOwnerCheck(guild_id=1, fetch_exc=exc)
    client = _connected_client_for_guilds([guild])

    assert await client.is_owner_in_guild("1", "42") is False


async def test_is_owner_in_guild_false_on_transient_exception_does_not_raise():
    """A transient failure (rate limit / network / forbidden) must fold to
    False, not propagate — this is the fail-closed guarantee the
    `owner_guild_only` gate (B2) depends on to never crash the forward
    path."""
    guild = _FakeGuildForOwnerCheck(guild_id=1, fetch_exc=RuntimeError("boom"))
    client = _connected_client_for_guilds([guild])

    assert await client.is_owner_in_guild("1", "42") is False


async def test_is_owner_in_guild_false_when_get_guild_returns_none():
    client = _connected_client_for_guilds([])  # guild "1" not in cache

    assert await client.is_owner_in_guild("1", "42") is False


async def test_is_owner_in_guild_false_when_not_connected():
    client = PycordClient(token="fake-token")  # run() never called, _bot is None

    assert await client.is_owner_in_guild("1", "42") is False


async def test_owner_guild_channels_returns_only_owner_guild_channels():
    owner_guild = _FakeGuildForOwnerCheck(
        guild_id=1, text_channel_ids=[101, 102]
    )
    stranger_guild = _FakeGuildForOwnerCheck(
        guild_id=2,
        fetch_exc=discord.NotFound(_FakeResponse(404), {"message": "Unknown Member"}),
        text_channel_ids=[201],
    )
    client = _connected_client_for_guilds([owner_guild, stranger_guild])

    channels = await client.owner_guild_channels("42")

    assert channels == ["101", "102"]


async def test_owner_guild_channels_empty_when_not_connected():
    client = PycordClient(token="fake-token")

    assert await client.owner_guild_channels("42") == []


def test_to_event_reply_to_bot_false_when_reference_unresolved():
    """A reply whose referenced message py-cord hasn't resolved (`resolved`
    is None — not in the internal cache) is treated as not-a-reply-to-bot
    rather than raising or triggering an extra fetch — a documented
    limitation (see `_to_event`'s docstring), not a crash."""
    bot = _ReplyBot(user_id=999)
    message = _ReplyMessage(reference=_ReplyReference(resolved=None))

    event = _to_event(message, bot)

    assert event["reply_to_bot"] is False
