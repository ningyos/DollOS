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

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Callback invoked for every incoming message event (plain dict — see
# `_to_event` below for the expected shape: author_id, is_dm, mentioned,
# reply_to_bot, content, channel_id, plus whatever else the adapter fills
# in. L0/L1 admission on this shape is now AttentionGate's job, daemon-side
# — see `dollos.mind.attention`).
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

    async def fetch_history(self, channel_id: str, limit: int) -> list[dict]:
        """Fetch up to `limit` recent messages from `channel_id` (Task 7:
        reconnect-gap backfill).

        Each returned dict is the same plain-dict event shape `on_message`
        delivers — `BridgeController` treats backfilled events identically
        to live ones — PLUS a true Discord `ts` (epoch
        seconds) and a `date` derived from it. Stamping `ts` is mandatory:
        `BridgeController._event_date` buckets AmbientLog's per-day file by
        `ts` when present specifically so a backfilled event near a UTC
        midnight boundary lands in the day it was actually posted, not the
        day the bridge happened to reconnect (LANDMINE, review).
        """
        ...

    async def is_owner_in_guild(self, guild_id: str, owner_id: str) -> bool:
        """Return True iff `owner_id` is a member of `guild_id` (Part B
        `owner_guild_only` detection primitive — B1; the gate itself is
        wired in B2).

        REST-only check (`fetch_member`) — requires NO Server Members
        Intent. Fail-closed on every ambiguous or failing path: guild not
        in the bot's cache, owner confirmed not a member, or ANY other
        exception (forbidden / rate-limited / network / not-yet-connected)
        all return False. This method NEVER raises — callers may rely on
        it always resolving to a bool.
        """
        ...


def _to_event(message: Any, bot: Any) -> dict:
    """Translate a py-cord Message into the plain-dict event shape the rest
    of the bridge (and the daemon-side `AttentionGate`) operate on.

    `message: discord.Message`, `bot: discord.Bot` — typed `Any` rather than
    `discord.*` because this is a MODULE-level function (shared by
    `PycordClient.run()`'s live `on_message` handler and
    `PycordClient.fetch_history()`, both of which call it after their own
    lazy `import discord`) and a module-level `discord.Message` annotation
    would need `discord` importable at module scope even under `from
    __future__ import annotations` (ruff's F821 flags the forward-ref name
    itself, regardless of runtime evaluation) — which is exactly what this
    module must NOT require.

    Stamps `ts` (the message's true Discord timestamp, epoch seconds) and
    `date` derived from it on every event — `BridgeController._event_date`
    needs a true post date to bucket AmbientLog by, live or backfilled (Task
    7 LANDMINE fix, review).

    Also stamps `reply_to_bot` (P1c whole-branch review Important #1) — the
    `l0_reply` L0 signal spec §3.4 requires (dm/mention/name/reply-to-her/
    always_wake). True iff `message` is a reply (`message.reference` set)
    AND py-cord has already resolved the referenced message's author to be
    this bot (`message.reference.resolved.author.id == bot.user.id`).
    py-cord populates `reference.resolved` directly from the same gateway/
    history payload that delivers `message` itself (Discord includes
    `referenced_message` inline) — no extra fetch needed on the live
    `on_message` or `fetch_history` paths this function serves. LIMITATION:
    if `resolved` is `None` (message not in py-cord's internal cache — rare,
    but possible) or a `DeletedReferencedMessage` (no `.author`), this
    conservatively reports `reply_to_bot=False` rather than issuing an extra
    fetch to resolve it — a reply to an already-deleted or uncached message
    is simply not recognized as `l0_reply` via this signal.
    """
    ts = message.created_at.timestamp()
    resolved = getattr(message.reference, "resolved", None) if message.reference else None
    resolved_author = getattr(resolved, "author", None)
    reply_to_bot = (
        bot.user is not None
        and resolved_author is not None
        and resolved_author.id == bot.user.id
    )
    return {
        "author_id": str(message.author.id),
        "is_dm": message.guild is None,
        "mentioned": bot.user is not None and bot.user in message.mentions,
        "reply_to_bot": reply_to_bot,
        "content": message.content,
        "channel_id": str(message.channel.id),
        "guild": str(message.guild.id) if message.guild else None,
        "channel": getattr(message.channel, "name", None),
        "author": str(message.author),
        "msg_id": str(message.id),
        "ts": ts,
        "date": datetime.fromtimestamp(ts, UTC).date().isoformat(),
    }


class PycordClient:
    """Real py-cord adapter implementing `DiscordClient`.

    py-cord is imported lazily inside each method that needs it (`run()`,
    `send()`) — constructing a `PycordClient` or calling
    `on_message()`/`me_id()`/`fetch_history()` before `run()` does NOT
    require py-cord to be importable at module scope.
    """

    def __init__(self, *, token: str) -> None:
        self._token = token
        self._bot = None
        self._on_message_cb: MessageCallback | None = None
        # Set once run() has assigned self._bot — the handoff signal
        # wait_until_ready() waits on (Task 7), instead of polling.
        self._bot_assigned = asyncio.Event()

    def on_message(self, cb: MessageCallback) -> None:
        self._on_message_cb = cb

    async def send(self, channel_id: str, text: str) -> None:
        """Send `text` to `channel_id`.

        Translates py-cord's real 429 into `RateLimited` (Task 7): py-cord's
        own HTTP client already retries ordinary rate limits internally
        (sleep-then-retry inside `discord.http.HTTPClient.request`) — an
        `HTTPException` with `status == 429` reaching here means that
        internal handling gave up (e.g. a Cloudflare-level block), so
        `BridgeController._send_with_retry`'s one explicit retry is the
        right place to decide what happens next, not a second silent retry
        buried here (CLAUDE.md: no fallback mechanisms — surface it, once,
        as `RateLimited`).
        """
        import discord

        if self._bot is None:
            raise RuntimeError("PycordClient.send() called before run() connected")
        channel = self._bot.get_channel(int(channel_id))
        if channel is None:
            channel = await self._bot.fetch_channel(int(channel_id))
        try:
            await channel.send(text)
        except discord.HTTPException as exc:
            if exc.status != 429:
                raise
            # Discord's 429 response always carries a `Retry-After` header
            # (seconds); py-cord's HTTPException doesn't surface the parsed
            # JSON body's `retry_after` as an attribute, so read the header.
            retry_after = float(exc.response.headers.get("Retry-After", 5))
            raise RateLimited(retry_after=retry_after) from exc

    def me_id(self) -> str:
        if self._bot is None or self._bot.user is None:
            raise RuntimeError("PycordClient.me_id() called before run() connected")
        return str(self._bot.user.id)

    async def fetch_history(self, channel_id: str, limit: int) -> list[dict]:
        """Fetch `channel_id`'s `limit` most recent messages (py-cord's
        `TextChannel.history()` default order), each translated by the same
        `_to_event` the live `on_message` handler uses — see the Protocol
        docstring for why `ts`/`date` stamping matters here."""
        if self._bot is None:
            raise RuntimeError(
                "PycordClient.fetch_history() called before run() connected"
            )
        channel = self._bot.get_channel(int(channel_id))
        if channel is None:
            channel = await self._bot.fetch_channel(int(channel_id))
        return [
            _to_event(message, self._bot)
            async for message in channel.history(limit=limit)
        ]

    async def is_owner_in_guild(self, guild_id: str, owner_id: str) -> bool:
        """REST-only membership check — see Protocol docstring.

        `discord.NotFound` from `fetch_member` means "confirmed not a
        member" -> False. Any other exception (Forbidden, HTTPException /
        429, network timeout, not-yet-connected, even a malformed
        `guild_id`/`owner_id`) is treated as transient/ambiguous and also
        folds to False (fail-closed, logged) — this method must never
        raise out, since a leaked exception here must not be allowed to
        crash the forward path that calls it (B2's call site additionally
        wraps this in try/except as defense in depth).
        """
        import discord

        if self._bot is None:
            logger.warning(
                "is_owner_in_guild called before run() connected "
                "(guild_id=%s) — fail-closed False",
                guild_id,
            )
            return False
        try:
            guild = self._bot.get_guild(int(guild_id))
            if guild is None:
                return False
            await guild.fetch_member(int(owner_id))
            return True
        except discord.NotFound:
            return False
        except Exception:
            logger.warning(
                "is_owner_in_guild: unexpected error checking guild_id=%s "
                "owner_id=%s — fail-closed False",
                guild_id,
                owner_id,
                exc_info=True,
            )
            return False

    async def wait_until_ready(self) -> None:
        """Block until `run()` has connected and py-cord's internal cache is
        ready (delegates to py-cord's own `Client.wait_until_ready()`).

        NOT part of the `DiscordClient` Protocol — `__main__.py` uses this
        directly on the concrete `PycordClient` it constructs, as the signal
        for "successful reconnect" before calling `reconnect_backfill` (Task
        7). `run()` assigns `self._bot` synchronously before it starts
        connecting, but `run()` itself only starts executing once the event
        loop schedules its task — `self._bot_assigned` is the handoff signal
        for that, rather than polling.
        """
        await self._bot_assigned.wait()
        await self._bot.wait_until_ready()

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
        self._bot_assigned.set()

        @bot.event
        async def on_message(message: discord.Message) -> None:
            if self._on_message_cb is None:
                return
            await self._on_message_cb(_to_event(message, bot))

        await bot.start(self._token)
