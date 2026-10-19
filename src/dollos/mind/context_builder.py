"""ContextBuilder — one append-only conversation per place (plan 2026-10-18
Task 9; harness S5, only the part MindLoop uses).

A conversation is a message list that only grows at the end: the fixed
system message, the snapshot that opened this page, then per turn one new
user message (``render_turn_delta``) followed by the assistant and ``tool``
messages exactly as they went out. Nothing earlier is ever re-rendered, so
each request is the previous one plus what was appended, and the server's
cache resumes from the previous request's end.

A new page (epoch) — the system message plus a code-written snapshot, no LLM
summary — opens only when:

1. the fixed prefix changed (tools JSON or system; e.g. current_self adopted
   a new version),
2. the last call's ``prompt_tokens + completion_tokens`` reached the budget
   (the server has no ``/tokenize``, so the last call's numbers are used),
3. the conversation is not in memory (daemon start, or dropped by the LRU),
4. the last call got a ``server`` error (a bad or too-long message would
   stay in the conversation and fail every call after it).

The turn kind, safe mode and idle time never open a page.

Keys: ``private`` is the origin-less bucket (the owner's local input, diary,
agenda, pulse, reflection, tool results); ``ch:<channel_id>`` one per
channel. At most ``MAX_CHANNEL_CONVERSATIONS`` channel conversations stay in
memory, the least recently used dropped first; ``private`` is never dropped.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from dollos.llm.model import End
from dollos.mind.mind_prompt import Seen

logger = logging.getLogger(__name__)

PRIVATE = "private"
MAX_CHANNEL_CONVERSATIONS = 8


def conversation_key(channel_id: str | None) -> str:
    """The conversation a turn of ``channel_id``'s bucket belongs to."""
    return f"ch:{channel_id}" if channel_id else PRIVATE


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tools_hash(tools: list[dict]) -> str:
    return text_hash(json.dumps(tools, ensure_ascii=False))


@dataclass
class Conversation:
    """One conversation's current page.

    ``key`` is None for a one-off request (the diary), which is never stored.
    ``prior_tokens`` is the last call's ``prompt_tokens + completion_tokens``:
    the next call's ``prior_tokens`` and the page-budget check; a call that
    reports no usage (error, interrupt) leaves it as it was.
    ``messages[:traced]`` are already in the trace (a page's ``epoch_start``
    covers the system message and the snapshot).
    """

    key: str | None
    epoch: int
    tools_hash: str
    system_hash: str
    messages: list[dict]
    seen: Seen = field(default_factory=Seen)
    prior_tokens: int = 0
    server_error: bool = False
    traced: int = 0

    @property
    def stream_key(self) -> tuple[str, int] | None:
        """``(key, epoch)`` for the adapter's call record; None for a one-off."""
        return (self.key, self.epoch) if self.key is not None else None

    def record_usage(self, end: End | None) -> None:
        if end is None or end.usage.prompt_tokens is None:
            return
        self.prior_tokens = end.usage.prompt_tokens + (end.usage.completion_tokens or 0)


def one_off(system: str, tools: list[dict], user: str) -> Conversation:
    """A request on its own message list, never stored: ``[system, user]``.
    Same system and tools as the private conversation, so the server's
    prefix resumes from it."""
    return Conversation(
        key=None,
        epoch=0,
        tools_hash=tools_hash(tools),
        system_hash=text_hash(system),
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )


def partial_reply(reasoning: str, spoken: str) -> dict | None:
    """The assistant message for a reply cut before it ended (interrupted, or
    the stream broke): the thinking so far and the sentences already handed
    to the sink — what was actually said — and no tool calls. None when
    nothing came out yet."""
    if not reasoning and not spoken:
        return None
    message: dict = {"role": "assistant", "content": spoken}
    if reasoning:
        message["reasoning_content"] = reasoning
    return message


class ContextBuilder:
    """The conversations of one MindLoop, in memory only."""

    def __init__(
        self, *, budget_tokens: int, max_channels: int = MAX_CHANNEL_CONVERSATIONS,
    ) -> None:
        if budget_tokens <= 0:
            raise ValueError(f"budget_tokens must be > 0, got {budget_tokens!r}")
        self._budget = budget_tokens
        self._max_channels = max_channels
        self._private: Conversation | None = None
        self._channels: OrderedDict[str, Conversation] = OrderedDict()

    def get(self, key: str) -> Conversation | None:
        if key == PRIVATE:
            return self._private
        return self._channels.get(key)

    def begin_turn(
        self,
        key: str,
        *,
        system: str,
        tools: list[dict],
        snapshot: Callable[[Seen], str],
    ) -> tuple[Conversation, str | None]:
        """``key``'s conversation for this turn, and why its page is new
        (None: it keeps growing). A new page is ``[system, user(snapshot(seen))]``
        with a fresh ``Seen``; ``snapshot`` is called only then."""
        t_hash, s_hash = tools_hash(tools), text_hash(system)
        conv = self.get(key)
        if conv is None:
            reason = "not in memory"
        elif (conv.tools_hash, conv.system_hash) != (t_hash, s_hash):
            reason = "fixed prefix changed"
        elif conv.prior_tokens >= self._budget:
            reason = f"last call used {conv.prior_tokens} >= {self._budget} tokens"
        elif conv.server_error:
            reason = "last call got a server error"
        else:
            self._touch(key)
            return conv, None
        seen = Seen()
        page = Conversation(
            key=key,
            epoch=conv.epoch + 1 if conv is not None else 1,
            tools_hash=t_hash,
            system_hash=s_hash,
            messages=[{"role": "system", "content": system}],
            seen=seen,
        )
        page.messages.append({"role": "user", "content": snapshot(seen)})
        page.traced = len(page.messages)
        self._store(page)
        logger.info("conversation %s: new page %d (%s)", key, page.epoch, reason)
        return page, reason

    def _touch(self, key: str) -> None:
        if key != PRIVATE:
            self._channels.move_to_end(key)

    def _store(self, conv: Conversation) -> None:
        if conv.key == PRIVATE:
            self._private = conv
            return
        self._channels[conv.key] = conv
        self._channels.move_to_end(conv.key)
        while len(self._channels) > self._max_channels:
            dropped, _ = self._channels.popitem(last=False)
            logger.info("conversation %s dropped (least recently used)", dropped)
