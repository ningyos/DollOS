"""DiscordClient protocol + py-cord adapter (P1b discord-bridge).

Discord I/O sits behind the `DiscordClient` Protocol so the rest of the
bridge (wake rules, ambient log, controller) is unit-testable without
py-cord or a real Discord connection — tests exercise the protocol via a
Fake implementation and never call `run()`. Real Discord is exercised only
in the live smoke, through `PycordClient`.

IMPORTANT: py-cord (imported as `discord`) is imported LAZILY, only inside
`PycordClient.run()`. Nothing at module import time touches py-cord, so
`import dollos.discord_bridge.client` (and anything that transitively
imports it) never requires py-cord to be installed.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

logger = logging.getLogger(__name__)

# Callback invoked for every incoming message event (plain dict — see
# wake.l0_wake for the expected shape: author_id, is_dm, mentioned,
# content, channel_id, plus whatever else the adapter fills in).
MessageCallback = Callable[[dict], Awaitable[None]]


class RateLimited(Exception):
    """Raised by `DiscordClient.send()` when Discord responds 429.

    `retry_after` is the delay in seconds Discord itself reports before the
    caller may retry (R2 finding: 429 must not be silently dropped, and must
    not be retried in an unbounded loop — `BridgeController` catches this and
    retries exactly once, see `controller.py`).
    """

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Discord rate limited: retry after {retry_after}s")
        self.retry_after = retry_after


class DiscordClient(Protocol):
    """Protocol for a Discord connection.

    Real implementation: `PycordClient` below. Tests implement this shape
    with a Fake (records sent messages, feeds synthetic events) so bridge
    logic never touches py-cord or the network.
    """

    def on_message(self, cb: MessageCallback) -> None:
        """Register the callback invoked for every incoming message event."""
        ...

    async def send(self, channel_id: str, text: str) -> None:
        """Send `text` to `channel_id`."""
        ...

    def me_id(self) -> str:
        """Return this bot's own Discord user id (self-filter target)."""
        ...

    async def run(self) -> None:
        """Connect and run the client loop until stopped or disconnected."""
        ...


class PycordClient:
    """Real py-cord adapter implementing `DiscordClient`.

    py-cord is imported lazily inside `run()` — constructing a
    `PycordClient` or calling `on_message()`/`me_id()` before `run()` does
    NOT require py-cord to be importable.
    """

    def __init__(self, *, token: str) -> None:
        self._token = token
        self._bot = None
        self._on_message_cb: MessageCallback | None = None

    def on_message(self, cb: MessageCallback) -> None:
        self._on_message_cb = cb

    async def send(self, channel_id: str, text: str) -> None:
        if self._bot is None:
            raise RuntimeError("PycordClient.send() called before run() connected")
        channel = self._bot.get_channel(int(channel_id))
        if channel is None:
            channel = await self._bot.fetch_channel(int(channel_id))
        await channel.send(text)

    def me_id(self) -> str:
        if self._bot is None or self._bot.user is None:
            raise RuntimeError("PycordClient.me_id() called before run() connected")
        return str(self._bot.user.id)

    async def run(self) -> None:
        """Connect to Discord and run until disconnected.

        py-cord ships under the `discord` import name; imported here, and
        only here, so the module stays importable without py-cord.
        """
        import discord

        intents = discord.Intents.default()
        intents.message_content = True
        bot = discord.Bot(intents=intents)
        self._bot = bot

        def to_event(message: discord.Message) -> dict:
            """Translate a py-cord Message into the plain-dict event shape
            `wake.l0_wake` and the rest of the bridge operate on."""
            return {
                "author_id": str(message.author.id),
                "is_dm": message.guild is None,
                "mentioned": bot.user is not None and bot.user in message.mentions,
                "content": message.content,
                "channel_id": str(message.channel.id),
                "guild": str(message.guild.id) if message.guild else None,
                "channel": getattr(message.channel, "name", None),
                "author": str(message.author),
                "msg_id": str(message.id),
            }

        @bot.event
        async def on_message(message: discord.Message) -> None:
            if self._on_message_cb is None:
                return
            await self._on_message_cb(to_event(message))

        await bot.start(self._token)
