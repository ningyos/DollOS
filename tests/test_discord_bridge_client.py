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

import discord
import pytest

from dollos.discord_bridge.client import PycordClient, RateLimited


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
