"""What the bridge does, apart from Discord's client and the HTTP transport (both injected).

- A message in: forwarded as an owner line only when it is a DM from the owner; every other
  message (another user's DM, any guild message, the owner's guild messages too) is dropped with
  one log line naming the author id and where, never the content. Her own messages are ignored
  without a log line. Attachments are text only for now: each becomes a line 「[附件：<filename>]」.
- What dollcore streams out: `say` goes to the owner's DM in pieces of at most 2000 characters,
  cut at line ends where possible; `typing` on/off keeps Discord's typing indicator up while her
  turn runs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Protocol

log = logging.getLogger("dollbridge")

#: Discord's limit for one message's content.
DISCORD_LIMIT = 2000
#: Discord shows "typing" for about 10 s per trigger; re-trigger a little sooner.
TYPING_EVERY_S = 8.0


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Pieces of at most `limit` characters, cut at line ends where possible; a line longer than
    `limit` is cut inside. Blank pieces are left out (Discord refuses empty messages)."""
    pieces: list[str] = []
    current: str | None = None
    for line in text.split("\n"):
        while len(line) > limit:
            if current is not None:
                pieces.append(current)
                current = None
            pieces.append(line[:limit])
            line = line[limit:]
        if current is None:
            current = line
        elif len(current) + 1 + len(line) <= limit:
            current += "\n" + line
        else:
            pieces.append(current)
            current = line
    if current is not None:
        pieces.append(current)
    return [p for p in pieces if p.strip()]


def owner_text(content: str, attachment_names: list[str]) -> str:
    """The owner line for one message: its text, then one 「[附件：…]」 line per attachment."""
    lines = [content] if content.strip() else []
    lines += [f"[附件：{name}]" for name in attachment_names]
    return "\n".join(lines)


class Core(Protocol):
    """dollcore's local API, as the bridge uses it."""

    async def post_owner(self, text: str) -> None: ...


class Channel(Protocol):
    """The owner's DM channel."""

    async def send(self, content: str) -> Any: ...

    async def trigger_typing(self) -> Any: ...


class Bridge:
    def __init__(self, owner_id: int, core: Core, self_id: Callable[[], int | None],
                 typing_every_s: float = TYPING_EVERY_S) -> None:
        self.owner_id = owner_id
        self.core = core
        self.self_id = self_id
        self.typing_every_s = typing_every_s
        self.dm: Channel | None = None
        self._typing: asyncio.Task[None] | None = None

    # Discord -> dollcore -------------------------------------------------------

    async def on_message(self, message: Any) -> None:
        author = message.author.id
        if author == self.self_id():
            return
        where = "DM" if message.guild is None else f"guild {message.guild.id} channel {message.channel.id}"
        if message.guild is not None or author != self.owner_id:
            log.info("dropped a message from user %s in %s (only the owner's DMs go to her)", author, where)
            return
        text = owner_text(message.content or "", [a.filename for a in message.attachments])
        if not text:
            log.info("dropped an empty owner DM (no text, no attachments)")
            return
        try:
            await self.core.post_owner(text)
        except Exception as e:  # noqa: BLE001 — every failure is told to the owner, then logged
            log.error("owner DM not delivered to dollcore: %s", e)
            await self._system(f"[系統] 這則沒送到她那裡：{e}")

    # dollcore -> Discord -------------------------------------------------------

    async def on_core(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "say":
            self._stop_typing()
            for piece in split_message(str(event.get("text", ""))):
                await self._send(piece)
        elif kind == "typing":
            if event.get("on"):
                self._start_typing()
            else:
                self._stop_typing()
        elif kind != "hello":
            log.warning("unknown event from dollcore: %r", event)

    async def _send(self, content: str) -> None:
        assert self.dm is not None, "the owner's DM channel is set before the stream starts"
        try:
            await self.dm.send(content)
        except Exception as e:  # noqa: BLE001
            # She is not told (her say already returned); the log line reaches dollcore's stderr.
            log.error("could not send to the owner's DM: %s", e)

    async def _system(self, content: str) -> None:
        if self.dm is not None:
            await self._send(content)

    def _start_typing(self) -> None:
        if self._typing is None or self._typing.done():
            self._typing = asyncio.create_task(self._keep_typing())

    def _stop_typing(self) -> None:
        if self._typing is not None:
            self._typing.cancel()
            self._typing = None

    async def _keep_typing(self) -> None:
        assert self.dm is not None
        while True:
            try:
                await self.dm.trigger_typing()
            except Exception as e:  # noqa: BLE001 — typing is cosmetic; say still goes out
                log.warning("typing indicator failed: %s", e)
            await asyncio.sleep(self.typing_every_s)


async def pump(events: Callable[[], Any], handle: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
    """Feeds every streamed event to `handle`; returns only by raising (the stream ending is an error)."""
    async for event in events():
        await handle(event)
    raise ConnectionError("dollcore closed the stream")
