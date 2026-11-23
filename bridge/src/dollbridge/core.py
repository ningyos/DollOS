"""What the bridge does, apart from Discord's client and the HTTP transport (both injected).

- A message in: a DM from the owner is an owner line. A DM from anyone else, or a message in the
  owner's channel (`channel_id`) that mentions her, is a stranger's line (2026-11-24) with their id,
  name and the channel's name (none for a DM); the mention itself is taken out of the text. Every
  other message (another guild channel, the channel without a mention, the owner in a guild) is
  dropped with one log line naming the author id and where, never the content. Her own messages are
  ignored without a log line. Attachments are text only for now: each becomes a line 「[附件：<filename>]」.
- What dollcore streams out: `say` goes to the owner's DM in pieces of at most 2000 characters,
  cut at line ends where possible; a `say` with `image` (a screenshot path on this Mac) carries it
  on its last piece; `typing` on/off keeps Discord's typing indicator up while her turn runs; `send`
  (her words to a stranger) goes to the owner's channel (`to` = `channel`) or to that user's DM
  (`to` = `dm:<id>`), in the same pieces.
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

    async def post_stranger(self, user_id: int, name: str, channel: str | None, text: str) -> None: ...


class Channel(Protocol):
    """The owner's DM channel."""

    async def send(self, content: str | None = None, *, file: Any = None) -> Any: ...

    async def trigger_typing(self) -> Any: ...


class Bridge:
    """`attach` turns a screenshot path from a `say` into what the channel's `send(file=…)` takes
    (`discord.File` in the real bridge)."""

    def __init__(self, owner_id: int, core: Core, self_id: Callable[[], int | None],
                 attach: Callable[[str], Any], typing_every_s: float = TYPING_EVERY_S,
                 channel_id: int | None = None,
                 open_dm: Callable[[int], Awaitable[Channel]] | None = None) -> None:
        self.owner_id = owner_id
        #: The owner's channel where strangers mention her (None: no channel) and that channel.
        self.channel_id = channel_id
        self.channel: Channel | None = None
        #: A user's DM channel, opened on demand for her words to a stranger.
        self.open_dm = open_dm
        self.core = core
        self.self_id = self_id
        self.attach = attach
        self.typing_every_s = typing_every_s
        self.dm: Channel | None = None
        self._typing: asyncio.Task[None] | None = None

    # Discord -> dollcore -------------------------------------------------------

    async def on_message(self, message: Any) -> None:
        author = message.author.id
        if author == self.self_id():
            return
        where = "DM" if message.guild is None else f"guild {message.guild.id} channel {message.channel.id}"
        if message.guild is None and author != self.owner_id:
            await self._stranger(message, None)
            return
        if message.guild is not None:
            me = self.self_id()
            mentioned = me is not None and any(m.id == me for m in getattr(message, "mentions", []))
            if self.channel_id is not None and message.channel.id == self.channel_id and mentioned:
                await self._stranger(message, getattr(message.channel, "name", str(message.channel.id)))
                return
            log.info("dropped a message from user %s in %s (not her channel, or no mention of her)", author, where)
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

    async def _stranger(self, message: Any, channel: str | None) -> None:
        """A stranger's line: their id, name, the channel's name (None for a DM), the text without
        the mention of her. Not delivered: logged, never told to them."""
        me = self.self_id()
        content = message.content or ""
        if me is not None:
            content = content.replace(f"<@{me}>", "").replace(f"<@!{me}>", "").strip()
        text = owner_text(content, [a.filename for a in message.attachments])
        if not text:
            return
        author = message.author
        name = getattr(author, "display_name", None) or getattr(author, "name", None) or str(author.id)
        try:
            await self.core.post_stranger(author.id, str(name), channel, text)
        except Exception as e:  # noqa: BLE001
            log.error("a stranger's line (user %s) not delivered to dollcore: %s", author.id, e)

    # dollcore -> Discord -------------------------------------------------------

    async def on_core(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "say":
            self._stop_typing()
            pieces = split_message(str(event.get("text", "")))
            image = event.get("image")
            for piece in pieces[:-1] if image else pieces:
                await self._send(piece)
            if image:
                # The screenshot of the window she last read (dollcore, 2026-11-16): with her last piece.
                await self._send_with(pieces[-1] if pieces else None, str(image))
        elif kind == "send":
            await self._send_to(str(event.get("to", "")), str(event.get("text", "")))
        elif kind == "typing":
            if event.get("on"):
                self._start_typing()
            else:
                self._stop_typing()
        elif kind != "hello":
            log.warning("unknown event from dollcore: %r", event)

    async def _send_to(self, to: str, text: str) -> None:
        """Her words to a stranger: the owner's channel, or a user's DM. A failure is logged."""
        try:
            if to == "channel":
                if self.channel is None:
                    raise RuntimeError("no channel is set (channel_id in discord.toml)")
                target = self.channel
            elif to.startswith("dm:") and to[3:].isdigit() and self.open_dm is not None:
                target = await self.open_dm(int(to[3:]))
            else:
                raise RuntimeError(f"unknown target {to!r}")
            for piece in split_message(text):
                await target.send(piece)
        except Exception as e:  # noqa: BLE001
            log.error("could not send to %s: %s", to, e)

    async def _send(self, content: str) -> None:
        assert self.dm is not None, "the owner's DM channel is set before the stream starts"
        try:
            await self.dm.send(content)
        except Exception as e:  # noqa: BLE001
            # She is not told (her say already returned); the log line reaches dollcore's stderr.
            log.error("could not send to the owner's DM: %s", e)

    async def _send_with(self, content: str | None, image: str) -> None:
        assert self.dm is not None, "the owner's DM channel is set before the stream starts"
        try:
            await self.dm.send(content, file=self.attach(image))
        except Exception as e:  # noqa: BLE001
            # Her words still go; the owner is told the picture did not.
            log.error("could not attach %s to the owner's DM: %s", image, e)
            if content:
                await self._send(content)
            await self._send(f"[系統] 截圖沒附上：{e}")

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
