"""Shared fakes + factory functions for dispatcher / cascade tests."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from dollos.character import Identity
from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.tool_outputs import ToolOutputStore
from dollos.tools import ToolCtx


def _doll_identity(self_: str = "You are Doll.") -> Identity:
    return Identity(self=self_, personality="- chill", taboos="- no LARP")


@dataclass
class _FakeAdapter(LLMAdapter):
    """Fake LLMAdapter — yields a configurable sequence of chunks.

    Captures call args for assertions. Records each call's keyword args
    in `self.calls`. For dispatcher (multi-message) tests the relevant
    entry is `calls[i]["messages"]`; legacy `stream_completion` callers
    populate `calls[i]["user"]` / `calls[i]["prefill"]` for back-compat
    with small-model code paths in tests.
    """

    chunks: list[StreamChunk] = field(default_factory=list)
    delay: float = 0.0
    calls: list[dict] = field(default_factory=list)

    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools: list[type] | None = None,
        grammar: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append(
            {"system": system, "user": user, "prefill": prefill, "tools": tools}
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        for c in self.chunks:
            yield c

    async def stream_messages(
        self,
        *,
        system: str,
        messages: list[dict],
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools: list[type] | None = None,
        grammar: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append(
            {"system": system, "messages": list(messages), "tools": tools}
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        for c in self.chunks:
            yield c


class _HangAdapter(LLMAdapter):
    """Adapter that hangs forever (for stop()/cancel tests)."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools: list[type] | None = None,
        grammar: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.entered.set()
        await asyncio.Event().wait()  # forever
        yield StreamChunk(text="", done=True)  # pragma: no cover

    async def stream_messages(
        self,
        *,
        system: str,
        messages: list[dict],
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools: list[type] | None = None,
        grammar: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.entered.set()
        await asyncio.Event().wait()  # forever
        yield StreamChunk(text="", done=True)  # pragma: no cover


class _FakeInnerVoice:
    """Fake InnerVoice.recall — returns a plain filtered string, captures args.

    Post 2026-05-08 wire format: recall returns plain text (no "RECALL:"
    prefix). Empty-string return signals "no relevant memory".
    """

    def __init__(
        self,
        recall_text: str = "- foo",
        raises: Exception | None = None,
    ) -> None:
        self._text = recall_text
        self._raises = raises
        self.calls: list[str] = []

    async def recall(self, query: str, **kwargs) -> str:
        self.calls.append(query)
        if self._raises is not None:
            raise self._raises
        return self._text


class _FakeInstinct:
    """Fake Instinct — captures process()/compact_cascade() calls.

    `summaries` controls process() return values (legacy path).
    `compact_summaries` controls compact_cascade() return values (active
    path, post 2026-05-09 rolling-compact). When exhausted, compact
    falls back to `f"summary {N}"` numbered by call count.
    `compact_raises` makes compact_cascade raise instead of returning.
    `raises` only applies to process() (not compact_cascade) so the
    "instinct should not be called" sentinel test still works.
    """

    def __init__(
        self,
        summaries: list[str] | None = None,
        raises: Exception | None = None,
        compact_summaries: list[str] | None = None,
        compact_raises: Exception | None = None,
    ) -> None:
        self._summaries = list(summaries) if summaries is not None else [""]
        self._raises = raises
        self._compact_summaries = (
            list(compact_summaries) if compact_summaries is not None else []
        )
        self._compact_raises = compact_raises
        self.calls: list[str] = []
        self.compact_calls: list[dict] = []

    async def process(self, event):  # type: ignore[no-untyped-def]
        self.calls.append(event.perception)
        if self._raises:
            raise self._raises
        if self._summaries:
            return self._summaries.pop(0)
        return ""

    async def compact_cascade(self, *, perception, cascade_messages):
        self.compact_calls.append({
            "perception": perception,
            "cascade_messages": list(cascade_messages),
        })
        if self._compact_raises is not None:
            raise self._compact_raises
        if self._compact_summaries:
            return self._compact_summaries.pop(0)
        return f"summary {len(self.compact_calls)}"


class _FakeMemSearch:
    def __init__(self, hits: list | None = None) -> None:
        self.indexed: list = []
        self._hits = hits or []

    async def index_file(self, path):
        self.indexed.append(path)

    async def search(self, query: str, top_k: int = 5):
        return self._hits


class _FakeCascadeLogger:
    """Records start_turn/log_iter calls for assertion."""

    def __init__(self) -> None:
        self.turn_ids: list[str] = []
        self.iters: list[dict] = []

    def start_turn(self) -> str:
        tid = f"fake-turn-{len(self.turn_ids) + 1}"
        self.turn_ids.append(tid)
        return tid

    def log_iter(self, **kwargs) -> None:
        self.iters.append(dict(kwargs))


def _make_tool_ctx(sink, memory_root, memsearch) -> ToolCtx:
    from dollos.scratchpad import Scratchpad

    return ToolCtx(
        sink=sink,
        memory_root=memory_root,
        memsearch=memsearch,
        transcripts_root=memory_root / "transcripts",
        tool_output_store=ToolOutputStore(memory_root / "tool_outputs"),
        scratchpad=Scratchpad(),
    )


def _make_dispatcher(
    *,
    adapter: LLMAdapter,
    inner_voice: _FakeInnerVoice,
    tmp_path: Path,
):
    from dollos.dispatcher import EventDispatcher
    from dollos.prompts import PromptRenderer

    from dollos.conversation_history import ConversationHistory
    from dollos.scratchpad import Scratchpad

    return EventDispatcher(
        adapter=adapter,
        inner_voice=inner_voice,
        instinct=_FakeInstinct(),
        renderer=PromptRenderer(),
        identity=_doll_identity(),
        memory_root=tmp_path,
        memsearch=_FakeMemSearch(),
        transcripts_root=tmp_path / "transcripts",
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
    )


async def _drain(sink: asyncio.Queue) -> list:
    items: list = []
    async with asyncio.timeout(1.0):
        while True:
            item = await sink.get()
            items.append(item)
            if item is None:
                return items
    return items  # pragma: no cover


def _think_with_mood(mood: str, tool_text: str = "ok") -> str:
    """Build an assistant emit containing a <think> block with a MOOD line
    and a Say tool call."""
    return (
        "<think>\n"
        "SEEN: 主人說了 hi\n"
        "INTENT: 打招呼\n"
        "REVIEW: first attempt\n"
        f"MOOD: {mood}\n"
        "TOOL: Say\n"
        "</think>\n\n"
        f'<tool_call>{{"name":"Say","arguments":{{"text":"{tool_text}"}}}}</tool_call>'
    )
