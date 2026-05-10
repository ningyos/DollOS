"""Tests for EventDispatcher — concurrent fan-out of RawEvents."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pytest
from pydantic import BaseModel

from dollos.character import Identity
from dollos.dispatcher import EventDispatcher, ToolResult


def _doll_identity(self_: str = "You are Doll.") -> Identity:
    return Identity(self=self_, personality="- chill", taboos="- no LARP")
from dollos.events import RawEvent, UserTextEvent
from dollos.ipc.messages import ErrorMsg, TextChunk, TurnEnd
from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.prompts import PromptRenderer
from dollos.tools import ToolCtx

# ----- Fakes -----


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
    return ToolCtx(
        sink=sink,
        memory_root=memory_root,
        memsearch=memsearch,
        transcripts_root=memory_root / "transcripts",
    )


def _make_dispatcher(
    *,
    adapter: LLMAdapter,
    inner_voice: _FakeInnerVoice,
    tmp_path: Path,
) -> EventDispatcher:
    return EventDispatcher(
        adapter=adapter,
        inner_voice=inner_voice,
        instinct=_FakeInstinct(),
        renderer=PromptRenderer(),
        identity=_doll_identity(),
        memory_root=tmp_path,
        memsearch=_FakeMemSearch(),
        transcripts_root=tmp_path / "transcripts",
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


# ----- Tests -----


@pytest.mark.asyncio
async def test_dispatch_is_sync_returns_immediately(tmp_path: Path):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"x"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ],
        delay=0.05,
    )
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    ev = UserTextEvent(text="hi", response_sink=sink)

    result = dispatcher.dispatch(ev)
    assert result is None
    assert len(dispatcher._tasks) == 1

    await _drain(sink)


@pytest.mark.asyncio
async def test_dispatch_pushes_chunks_then_turnend_then_none_sentinel(tmp_path: Path):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text=(
                    '<tool_call>{"name":"Say","arguments":{"text":"Hi"}}</tool_call>'
                    '<tool_call>{"name":"Say","arguments":{"text":" there"}}</tool_call>'
                ),
                done=False,
            ),
            StreamChunk(text="", done=True),
        ],
    )
    iv = _FakeInnerVoice("RECALL:\n- foo\n")
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(UserTextEvent(text="hi", response_sink=sink))

    items = await _drain(sink)
    assert items == [
        TextChunk(text="Hi"),
        TextChunk(text=" there"),
        TurnEnd(),
        None,
    ]


@pytest.mark.asyncio
async def test_recall_result_wraps_user_message_with_memory_context(tmp_path: Path):
    """IV.recall result is wrapped in [Memory context] block prepended to
    user message (RAG context pattern, 2026-05-08). Prefill stays empty."""
    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice("- user likes coffee")
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(UserTextEvent(text="hello world", response_sink=sink))

    await _drain(sink)

    assert iv.calls == ["hello world"]
    assert len(adapter.calls) == 1
    user = adapter.calls[0]["messages"][0]["content"]
    assert "[Memory context]" in user
    assert "- user likes coffee" in user
    assert "[Message]" in user
    assert "hello world" in user
    assert "RECALL:" not in user


@pytest.mark.asyncio
async def test_empty_recall_still_emits_memory_context_block(tmp_path: Path):
    """Empty IV.recall result still produces an explicit (no relevant memory)
    line in the [Memory context] block."""
    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice("")  # no hits
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(UserTextEvent(text="hi", response_sink=sink))

    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    assert "[Memory context]" in user
    assert "(no relevant memory)" in user
    assert "[Message]" in user
    assert "hi" in user


@pytest.mark.asyncio
async def test_handler_exception_pushes_errormsg_and_sentinel(tmp_path: Path):
    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice(raises=RuntimeError("boom"))
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(UserTextEvent(text="x", response_sink=sink))

    items = await _drain(sink)
    assert len(items) == 2
    assert isinstance(items[0], ErrorMsg)
    assert "boom" in items[0].message
    assert items[1] is None


@pytest.mark.asyncio
async def test_perceive_typeerror_for_unsupported_raw_logged(tmp_path: Path, caplog):
    """An unsupported RawEvent subclass: _sink_of raises TypeError; task dies
    with a logged exception. No sink to push to."""

    class FooEvent(RawEvent):
        pass

    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    with caplog.at_level(logging.ERROR, logger="dollos.dispatcher"):
        dispatcher.dispatch(FooEvent())
        for _ in range(5):
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)

    assert len(dispatcher._tasks) == 0
    assert any(
        "no sink" in rec.message.lower() or "typeerror" in rec.message.lower()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_stop_cancels_in_flight_tasks(tmp_path: Path):
    hang = _HangAdapter()
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=hang, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(UserTextEvent(text="hang", response_sink=sink))

    await asyncio.wait_for(hang.entered.wait(), timeout=1.0)

    t0 = time.monotonic()
    await asyncio.wait_for(dispatcher.stop(), timeout=1.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5
    assert len(dispatcher._tasks) == 0


@pytest.mark.asyncio
async def test_dispatch_after_stop_raises_runtime_error(tmp_path: Path):
    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    await dispatcher.stop()

    sink: asyncio.Queue = asyncio.Queue()
    with pytest.raises(RuntimeError):
        dispatcher.dispatch(UserTextEvent(text="x", response_sink=sink))


@pytest.mark.asyncio
async def test_concurrent_dispatch_runs_in_parallel(tmp_path: Path):
    """Phase 1 schedule: serializable events queue, but parallel-typed
    events (SubagentResultEvent) still dispatch concurrently.

    Updated 2026-05-10 — earlier version dispatched two UserTextEvents and
    expected parallel; the new dispatcher serializes user-facing events so
    we exercise the parallel path with SubagentResultEvent + UserTextEvent.
    """
    from dollos.events import SubagentResultEvent

    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"x"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ],
        delay=0.05,
    )
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink_a: asyncio.Queue = asyncio.Queue()
    sink_b: asyncio.Queue = asyncio.Queue()

    t0 = time.monotonic()
    dispatcher.dispatch(UserTextEvent(text="a", response_sink=sink_a))
    dispatcher.dispatch(SubagentResultEvent(
        subagent_id="x", task="t", status="ok",
        summary="s", details="d", response_sink=sink_b,
    ))

    items_a, items_b = await asyncio.gather(_drain(sink_a), _drain(sink_b))
    elapsed = time.monotonic() - t0

    assert any(isinstance(it, TextChunk) for it in items_a)
    assert any(isinstance(it, TextChunk) for it in items_b)
    assert elapsed < 0.09, f"elapsed {elapsed:.3f}s — looks serial"


@pytest.mark.asyncio
async def test_dispatcher_does_not_call_instinct_process(tmp_path: Path):
    """Instinct.process is no longer called from the dispatcher hot path
    (post 2026-05-08). Class still constructible (kernel builds it), just
    not consumed here."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    # Instinct that would raise if called — proves dispatcher never calls it.
    inst = _FakeInstinct(raises=RuntimeError("instinct should not be called"))
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=PromptRenderer(),
        identity=_doll_identity(),
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))

    items = []
    while True:
        item = await sink.get()
        if item is None:
            break
        items.append(item)

    assert inst.calls == []
    # Adapter still ran (no instinct error surfaced).
    assert len(adapter.calls) == 1
    assert any(isinstance(m, TextChunk) for m in items)


@pytest.mark.asyncio
async def test_dispatcher_uses_stream_messages_not_stream_completion(tmp_path: Path):
    """Cascade flow uses the new multi-message stream_messages API
    (2026-05-08); legacy stream_completion is reserved for small-model
    callers (InnerVoice / Instinct)."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice("- foo")
    inst = _FakeInstinct(summaries=["主人剛打招呼。"])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=PromptRenderer(),
        identity=_doll_identity(),
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        item = await sink.get()
        if item is None:
            break

    assert len(adapter.calls) == 1
    # Multi-message API used: call carries `messages`, not `user`/`prefill`.
    assert "messages" in adapter.calls[0]
    assert "user" not in adapter.calls[0]


@pytest.mark.asyncio
async def test_dispatcher_routes_say_tool_call_to_text_chunk(tmp_path: Path):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"hello"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice(recall_text="RECALL:\n- foo\n")
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=PromptRenderer(),
        identity=_doll_identity(),
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))

    items = []
    while True:
        item = await sink.get()
        if item is None:
            break
        items.append(item)

    text_chunks = [m for m in items if isinstance(m, TextChunk)]
    assert any(m.text == "hello" for m in text_chunks)
    assert any(isinstance(m, TurnEnd) for m in items)


@pytest.mark.asyncio
async def test_dispatcher_routes_note_memory_tool_call(tmp_path: Path):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text=(
                    '<tool_call>{"name":"NoteMemory","arguments":'
                    '{"text":"likes coffee"}}</tool_call>'
                ),
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice(recall_text="RECALL:\n- foo\n")
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=PromptRenderer(),
        identity=_doll_identity(),
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        item = await sink.get()
        if item is None:
            break

    shared = tmp_path / "shared"
    files = list(shared.glob("*.md"))
    assert len(files) == 1
    assert files[0].read_text().endswith("- likes coffee\n")
    assert ms.indexed and Path(ms.indexed[0]) == files[0]


@pytest.mark.asyncio
async def test_dispatcher_executes_multiple_tool_calls_in_order(tmp_path: Path):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text=(
                    '<tool_call>{"name":"NoteMemory","arguments":'
                    '{"text":"a"}}</tool_call>'
                    '<tool_call>{"name":"Say","arguments":'
                    '{"text":"b"}}</tool_call>'
                ),
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    items = []
    while True:
        m = await sink.get()
        if m is None:
            break
        items.append(m)

    assert (tmp_path / "shared").exists()
    assert any(isinstance(m, TextChunk) and m.text == "b" for m in items)


@pytest.mark.asyncio
async def test_dispatcher_naked_text_is_dropped(tmp_path: Path):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text="leaked thinking text\n", done=False),
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"x"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="trailing leak\n", done=False),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    items = []
    while True:
        m = await sink.get()
        if m is None:
            break
        items.append(m)

    text_chunks = [m for m in items if isinstance(m, TextChunk)]
    assert len(text_chunks) == 1
    assert text_chunks[0].text == "x"


@pytest.mark.asyncio
async def test_dispatcher_unknown_tool_logs_and_skips(tmp_path: Path, caplog):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"WhoKnows","arguments":{}}</tool_call>',
                done=False,
            ),
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"after"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    with caplog.at_level("WARNING"):
        disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
        items = []
        while True:
            m = await sink.get()
            if m is None:
                break
            items.append(m)

    text_chunks = [m for m in items if isinstance(m, TextChunk)]
    assert any(m.text == "after" for m in text_chunks)
    assert any("WhoKnows" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_dispatcher_validation_error_logs_and_skips(tmp_path: Path, caplog):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"wrong":"k"}}</tool_call>',
                done=False,
            ),
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    with caplog.at_level("WARNING"):
        disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
        items = []
        while True:
            m = await sink.get()
            if m is None:
                break
            items.append(m)

    text_chunks = [m for m in items if isinstance(m, TextChunk)]
    assert any(m.text == "ok" for m in text_chunks)
    assert any("validation" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_dispatcher_passes_tools_to_adapter(tmp_path: Path):
    adapter = _FakeAdapter(
        chunks=[StreamChunk(text="", done=True)]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    assert len(adapter.calls) == 1
    from dollos.tools import TOOLS
    assert adapter.calls[0].get("tools") == TOOLS


@pytest.mark.asyncio
async def test_dispatch_tool_call_returns_none_on_success(tmp_path):
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=_FakeAdapter(chunks=[]),
        inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_tool_ctx(sink, tmp_path, ms)

    fail = await disp._dispatch_tool_call(
        {"name": "Say", "arguments": {"text": "hi"}}, ctx
    )

    assert fail is None
    msg = sink.get_nowait()
    assert isinstance(msg, TextChunk)
    assert msg.text == "hi"


@pytest.mark.asyncio
async def test_dispatch_tool_call_returns_failure_on_unknown_tool(tmp_path):
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=_FakeAdapter(chunks=[]),
        inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_tool_ctx(sink, tmp_path, ms)

    fail = await disp._dispatch_tool_call(
        {"name": "WhoKnows", "arguments": {}}, ctx
    )

    assert isinstance(fail, ToolResult) and not fail.success
    assert fail.tool_name == "WhoKnows"
    assert "unknown" in fail.detail.lower()


@pytest.mark.asyncio
async def test_dispatch_tool_call_returns_failure_on_validation_error(tmp_path):
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=_FakeAdapter(chunks=[]),
        inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_tool_ctx(sink, tmp_path, ms)

    fail = await disp._dispatch_tool_call(
        {"name": "Say", "arguments": {"wrong": "k"}}, ctx
    )

    assert isinstance(fail, ToolResult) and not fail.success
    assert fail.tool_name == "Say"
    assert "validation" in fail.detail.lower()


@pytest.mark.asyncio
async def test_dispatch_tool_call_returns_failure_and_emits_errormsg_on_runtime_error(tmp_path):
    class _BoomTool(BaseModel):
        text: str
        async def run(self, ctx):
            raise RuntimeError("kaboom")

    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=_FakeAdapter(chunks=[]),
        inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    disp._tools_by_name["_BoomTool"] = _BoomTool
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_tool_ctx(sink, tmp_path, ms)

    fail = await disp._dispatch_tool_call(
        {"name": "_BoomTool", "arguments": {"text": "x"}}, ctx
    )

    assert isinstance(fail, ToolResult) and not fail.success
    assert "kaboom" in fail.detail
    msg = sink.get_nowait()
    assert isinstance(msg, ErrorMsg)
    assert "_BoomTool" in msg.message and "kaboom" in msg.message


@pytest.mark.asyncio
async def test_dispatch_tool_call_non_string_name_returns_failure(tmp_path):
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=_FakeAdapter(chunks=[]),
        inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_tool_ctx(sink, tmp_path, ms)

    fail = await disp._dispatch_tool_call({"name": 42, "arguments": {}}, ctx)

    assert isinstance(fail, ToolResult) and not fail.success
    assert "name" in fail.detail.lower()


@pytest.mark.asyncio
async def test_respond_cascades_after_unknown_tool(tmp_path: Path):
    """First adapter call yields unknown tool; second yields valid Say."""

    class _RoundedFakeAdapter:
        def __init__(self, rounds):
            self._rounds = rounds
            self.calls: list[dict] = []

        async def stream_messages(
            self, *, system, messages, stop=None,
            max_tokens=1024, tools=None, grammar=None,
        ):
            idx = len(self.calls)
            self.calls.append(
                {"system": system, "messages": list(messages), "tools": tools}
            )
            chunks = self._rounds[idx]
            for c in chunks:
                yield c

    rounds = [
        [
            StreamChunk(
                text='<tool_call>{"name":"WhoKnows","arguments":{}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ],
        [
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"fixed"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ],
    ]
    adapter = _RoundedFakeAdapter(rounds)
    iv = _FakeInnerVoice(recall_text="RECALL:\n- foo\n")
    inst = _FakeInstinct(summaries=["", ""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    items = []
    while True:
        m = await sink.get()
        if m is None:
            break
        items.append(m)

    assert len(adapter.calls) == 2
    # Second call's messages list reflects the failed unknown-tool round:
    # [user(framed), assistant(model emit), user(<tool_response>...unknown...)]
    second_msgs = adapter.calls[1]["messages"]
    assert len(second_msgs) == 3
    assert second_msgs[0]["role"] == "user"
    assert "[Message]" in second_msgs[0]["content"]
    assert second_msgs[1]["role"] == "assistant"
    assert second_msgs[2]["role"] == "user"
    assert "<tool_response>" in second_msgs[2]["content"]
    assert "unknown" in second_msgs[2]["content"].lower()
    text_chunks = [m for m in items if isinstance(m, TextChunk)]
    assert any(m.text == "fixed" for m in text_chunks)
    assert any(isinstance(m, TurnEnd) for m in items)


@pytest.mark.asyncio
async def test_respond_no_cascade_when_no_fails(tmp_path: Path):
    """Step 6 behavior preserved — no fails, single round, immediate TurnEnd."""

    class _OneShotAdapter:
        def __init__(self, chunks):
            self._chunks = chunks
            self.calls: list[dict] = []

        async def stream_messages(
            self, *, system, messages, stop=None,
            max_tokens=1024, tools=None, grammar=None,
        ):
            self.calls.append({"messages": list(messages)})
            for c in self._chunks:
                yield c

    adapter = _OneShotAdapter([
        StreamChunk(
            text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
            done=False,
        ),
        StreamChunk(text="", done=True),
    ])
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_respond_cascade_perception_includes_multiple_fails(tmp_path: Path):
    """One round emits two fails; next round perception lists both."""

    class _TwoRoundAdapter:
        def __init__(self):
            self.calls = []

        async def stream_messages(self, **kw):
            self.calls.append({**kw, "messages": list(kw["messages"])} if "messages" in kw else kw)
            idx = len(self.calls) - 1
            if idx == 0:
                yield StreamChunk(
                    text=(
                        '<tool_call>{"name":"A","arguments":{}}</tool_call>'
                        '<tool_call>{"name":"B","arguments":{}}</tool_call>'
                    ),
                    done=False,
                )
                yield StreamChunk(text="", done=True)
            else:
                yield StreamChunk(
                    text='<tool_call>{"name":"Say","arguments":{"text":"done"}}</tool_call>',
                    done=False,
                )
                yield StreamChunk(text="", done=True)

    adapter = _TwoRoundAdapter()
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=["", ""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    # Round 2 must include both A and B tool_response messages.
    second_msgs = adapter.calls[1]["messages"]
    tool_responses = [m for m in second_msgs if "<tool_response>" in m.get("content", "")]
    # Two unknown-tool failures from round 1 each produce a tool_response.
    assert len(tool_responses) == 2
    joined = "\n".join(m["content"] for m in tool_responses)
    assert "unknown" in joined.lower()


@pytest.mark.asyncio
async def test_dispatcher_writes_user_text_transcript_after_turn(tmp_path: Path):
    """User text is written to transcript in finally, after the turn completes."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    transcripts_root = tmp_path / "transcripts"
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=transcripts_root,
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    expected = transcripts_root / f"{date.today():%Y-%m-%d}.md"
    assert expected.exists()
    content = expected.read_text()
    assert "主人說：hi" in content
    assert "我說：ok" in content


@pytest.mark.asyncio
async def test_dispatcher_handles_diary_event(tmp_path: Path):
    """DiaryEvent flows through perceive/respond pipeline; perception
    tells Doll to write diary; daily.md ends up with diary section."""

    captured_user_message: list[str] = []

    class _CaptureAdapter:
        def __init__(self):
            self.calls = []

        async def stream_messages(self, **kw):
            self.calls.append({**kw, "messages": list(kw["messages"])} if "messages" in kw else kw)
            captured_user_message.append(kw["messages"][0]["content"])
            yield StreamChunk(
                text=(
                    '<tool_call>{"name":"WriteDiary","arguments":'
                    '{"content":"today felt good"}}</tool_call>'
                ),
                done=False,
            )
            yield StreamChunk(text="", done=True)

    from dollos.events import DiaryEvent
    adapter = _CaptureAdapter()
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    transcripts_root = tmp_path / "transcripts"
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=transcripts_root,
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(DiaryEvent(response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    # The perception told Doll to write a diary
    assert "日記" in captured_user_message[0]
    # WriteDiary tool was actually called → daily file has diary section
    daily_file = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
    assert daily_file.exists()
    assert "## 日記 (" in daily_file.read_text()


def test_tool_result_dataclass_fields():
    r = ToolResult(tool_name="X", success=False, detail="boom")
    assert r.tool_name == "X"
    assert r.success is False
    assert r.detail == "boom"


def test_tool_result_success_field_defaults_to_required():
    """success must be explicit (no default) — caller intent should be visible."""
    import dataclasses
    fields = {f.name: f for f in dataclasses.fields(ToolResult)}
    assert "success" in fields
    assert fields["success"].default is dataclasses.MISSING


@pytest.mark.asyncio
async def test_dispatch_tool_call_returns_none_when_tool_run_returns_none(tmp_path):
    """Side-effect tool (Say) returning None → _dispatch_tool_call returns None (no cascade)."""
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=_FakeAdapter(chunks=[]),
        inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_tool_ctx(sink, tmp_path, ms)

    result = await disp._dispatch_tool_call(
        {"name": "Say", "arguments": {"text": "hi"}}, ctx
    )

    assert result is None


@pytest.mark.asyncio
async def test_dispatch_tool_call_returns_success_result_when_tool_returns_str(tmp_path):
    """Returning tool returning str → ToolResult(success=True, detail=str)."""

    class _ReturningTool(BaseModel):
        text: str
        async def run(self, ctx) -> str:
            return f"echoed: {self.text}"

    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=_FakeAdapter(chunks=[]),
        inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    disp._tools_by_name["_ReturningTool"] = _ReturningTool
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_tool_ctx(sink, tmp_path, ms)

    result = await disp._dispatch_tool_call(
        {"name": "_ReturningTool", "arguments": {"text": "hi"}}, ctx
    )

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.detail == "echoed: hi"


@pytest.mark.asyncio
async def test_dispatch_tool_call_returns_success_result_with_empty_str(tmp_path):
    """Returning tool returning empty str → ToolResult(success=True, detail='')."""

    class _EmptyReturningTool(BaseModel):
        async def run(self, ctx) -> str:
            return ""

    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=_FakeAdapter(chunks=[]),
        inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    disp._tools_by_name["_EmptyReturningTool"] = _EmptyReturningTool
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_tool_ctx(sink, tmp_path, ms)

    result = await disp._dispatch_tool_call(
        {"name": "_EmptyReturningTool", "arguments": {}}, ctx
    )

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.detail == ""


@pytest.mark.asyncio
async def test_respond_cascades_success_with_returning_tool(tmp_path: Path):
    """Round 1: returning tool fires → cascade → Round 2: Say wraps up."""

    class _RoundedFakeAdapter:
        def __init__(self, rounds):
            self._rounds = rounds
            self.calls: list[dict] = []

        async def stream_messages(
            self, *, system, messages, stop=None,
            max_tokens=1024, tools=None, grammar=None,
        ):
            idx = len(self.calls)
            self.calls.append(
                {"system": system, "messages": list(messages), "tools": tools}
            )
            for c in self._rounds[idx]:
                yield c

    class _Echo(BaseModel):
        text: str
        async def run(self, ctx) -> str:
            return f"got: {self.text}"

    rounds = [
        [
            StreamChunk(
                text='<tool_call>{"name":"_Echo","arguments":{"text":"hi"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ],
        [
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"done"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ],
    ]
    adapter = _RoundedFakeAdapter(rounds)
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=["", ""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    disp._tools_by_name["_Echo"] = _Echo

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    items = []
    while True:
        m = await sink.get()
        if m is None:
            break
        items.append(m)

    assert len(adapter.calls) == 2
    # Round 2 messages: original user, assistant emit, tool_response.
    second_msgs = adapter.calls[1]["messages"]
    assert len(second_msgs) == 3
    assert second_msgs[2]["role"] == "user"
    assert "<tool_response>" in second_msgs[2]["content"]
    assert "got: hi" in second_msgs[2]["content"]
    text_chunks = [m for m in items if isinstance(m, TextChunk)]
    assert any(m.text == "done" for m in text_chunks)


@pytest.mark.asyncio
async def test_respond_cascades_success_with_empty_str_perception(tmp_path: Path):
    """Empty-string success cascade: perception says '成功，無輸出'."""

    class _RoundedFakeAdapter:
        def __init__(self, rounds):
            self._rounds = rounds
            self.calls: list[dict] = []

        async def stream_messages(
            self, *, system, messages, stop=None,
            max_tokens=1024, tools=None, grammar=None,
        ):
            idx = len(self.calls)
            self.calls.append(
                {"system": system, "messages": list(messages), "tools": tools}
            )
            for c in self._rounds[idx]:
                yield c

    class _Empty(BaseModel):
        async def run(self, ctx) -> str:
            return ""

    rounds = [
        [
            StreamChunk(
                text='<tool_call>{"name":"_Empty","arguments":{}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ],
        [
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ],
    ]
    adapter = _RoundedFakeAdapter(rounds)
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=["", ""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    disp._tools_by_name["_Empty"] = _Empty

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    # Round 2 messages: original user, assistant emit, tool_response
    # whose content is "(no output)" (since _Empty returned "").
    second_msgs = adapter.calls[1]["messages"]
    assert len(second_msgs) == 3
    tr = second_msgs[2]
    assert tr["role"] == "user"
    assert "<tool_response>" in tr["content"]
    assert "(no output)" in tr["content"]


@pytest.mark.asyncio
async def test_cascade_breaks_on_same_tool_consecutive_3_failures(tmp_path: Path):
    """3 consecutive InvokeSkill failures -> ErrorMsg + cascade aborts."""

    class _RoundedAdapter:
        def __init__(self):
            self.calls: list[dict] = []

        async def stream_messages(self, **kw):
            self.calls.append({**kw, "messages": list(kw["messages"])} if "messages" in kw else kw)
            yield StreamChunk(
                text=(
                    '<tool_call>{"name":"InvokeSkill","arguments":'
                    '{"wrong":"x"}}</tool_call>'
                ),
                done=False,
            )
            yield StreamChunk(text="", done=True)

    adapter = _RoundedAdapter()
    iv = _FakeInnerVoice()
    disp = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    items = await _drain(sink)

    # 3 rounds, then ErrorMsg + break.
    assert len(adapter.calls) == 3
    assert any(
        isinstance(m, ErrorMsg)
        and "連續 3 次 InvokeSkill" in m.message
        and "停下來換思路" in m.message
        for m in items
    )
    assert any(isinstance(m, TurnEnd) for m in items)


@pytest.mark.asyncio
async def test_cascade_resets_consecutive_counter_on_success(tmp_path: Path):
    """Fail, fail, success, fail -> does NOT trigger same-tool break.

    After the third success, the cascade keeps going; we cap iterations
    by yielding a final Say so the loop ends naturally.
    """

    class _ScriptedAdapter:
        def __init__(self):
            self.calls: list[dict] = []

        async def stream_messages(self, **kw):
            idx = len(self.calls)
            self.calls.append({**kw, "messages": list(kw["messages"])} if "messages" in kw else kw)
            scripts = [
                # round 0: fail (validation)
                '<tool_call>{"name":"InvokeSkill","arguments":{"bad":"x"}}</tool_call>',
                # round 1: fail (validation)
                '<tool_call>{"name":"InvokeSkill","arguments":{"bad":"y"}}</tool_call>',
                # round 2: success-cascade (Recall returns str)
                '<tool_call>{"name":"Recall","arguments":{"query":"q"}}</tool_call>',
                # round 3: fail again — counter was reset on success → 1, not 3
                '<tool_call>{"name":"InvokeSkill","arguments":{"bad":"z"}}</tool_call>',
                # round 4: terminate with Say (returns None → no cascade)
                '<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
            ]
            text = scripts[min(idx, len(scripts) - 1)]
            yield StreamChunk(text=text, done=False)
            yield StreamChunk(text="", done=True)

    adapter = _ScriptedAdapter()
    iv = _FakeInnerVoice()
    disp = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    items = await _drain(sink)

    # No same-tool ErrorMsg; loop ended via Say-returns-None on round 4.
    assert not any(
        isinstance(m, ErrorMsg) and "連續 3 次" in m.message
        for m in items
    )
    assert len(adapter.calls) == 5
    assert any(isinstance(m, TextChunk) and m.text == "ok" for m in items)


@pytest.mark.asyncio
async def test_cascade_does_not_break_on_alternating_tool_failures(tmp_path: Path):
    """A-fail, B-fail, A-fail, then Say -> no break (counter reset on
    different tool name each time)."""

    class _ScriptedAdapter:
        def __init__(self):
            self.calls: list[dict] = []

        async def stream_messages(self, **kw):
            idx = len(self.calls)
            self.calls.append({**kw, "messages": list(kw["messages"])} if "messages" in kw else kw)
            scripts = [
                '<tool_call>{"name":"WhoKnowsA","arguments":{}}</tool_call>',
                '<tool_call>{"name":"WhoKnowsB","arguments":{}}</tool_call>',
                '<tool_call>{"name":"WhoKnowsA","arguments":{}}</tool_call>',
                '<tool_call>{"name":"Say","arguments":{"text":"done"}}</tool_call>',
            ]
            text = scripts[min(idx, len(scripts) - 1)]
            yield StreamChunk(text=text, done=False)
            yield StreamChunk(text="", done=True)

    adapter = _ScriptedAdapter()
    iv = _FakeInnerVoice()
    disp = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    items = await _drain(sink)

    assert not any(
        isinstance(m, ErrorMsg) and "連續 3 次" in m.message
        for m in items
    )
    assert len(adapter.calls) == 4
    assert any(isinstance(m, TextChunk) and m.text == "done" for m in items)


@pytest.mark.asyncio
async def test_cascade_preserves_original_user_in_messages_first(tmp_path: Path):
    """Across cascade iterations, messages[0] stays the original framed
    user perception — never overwritten."""

    class _Echo(BaseModel):
        text: str
        async def run(self, ctx) -> str:
            return f"echo:{self.text}"

    class _ScriptedAdapter:
        def __init__(self):
            self.calls: list[dict] = []

        async def stream_messages(self, **kw):
            idx = len(self.calls)
            self.calls.append({**kw, "messages": list(kw["messages"])} if "messages" in kw else kw)
            scripts = [
                '<tool_call>{"name":"_Echo","arguments":{"text":"a"}}</tool_call>',
                '<tool_call>{"name":"Say","arguments":{"text":"final"}}</tool_call>',
            ]
            text = scripts[min(idx, len(scripts) - 1)]
            yield StreamChunk(text=text, done=False)
            yield StreamChunk(text="", done=True)

    adapter = _ScriptedAdapter()
    iv = _FakeInnerVoice()
    disp = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    disp._tools_by_name["_Echo"] = _Echo

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="原始問題", response_sink=sink))
    await _drain(sink)

    assert len(adapter.calls) == 2
    for call in adapter.calls:
        first = call["messages"][0]
        assert first["role"] == "user"
        assert "[Message]\n原始問題" in first["content"]


@pytest.mark.asyncio
async def test_cascade_appends_assistant_then_tool_response(tmp_path: Path):
    """After 1 iteration with a successful returning tool, the next
    iteration's messages list is [user, assistant(raw model emit),
    user(<tool_response>...)]."""

    class _Path(BaseModel):
        async def run(self, ctx) -> str:
            return "/path/X"

    raw_emit = (
        "SEEN: ok\nINTENT: pwd\nTOOL: _Path\n</think>\n\n"
        '<tool_call>{"name":"_Path","arguments":{}}</tool_call>'
    )

    class _ScriptedAdapter:
        def __init__(self):
            self.calls: list[dict] = []

        async def stream_messages(self, **kw):
            idx = len(self.calls)
            # Snapshot messages list — dispatcher continues mutating it.
            snap = {**kw, "messages": list(kw["messages"])}
            self.calls.append(snap)
            if idx == 0:
                yield StreamChunk(text=raw_emit, done=False)
                yield StreamChunk(text="", done=True)
            else:
                yield StreamChunk(
                    text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                    done=False,
                )
                yield StreamChunk(text="", done=True)

    adapter = _ScriptedAdapter()
    iv = _FakeInnerVoice()
    disp = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    disp._tools_by_name["_Path"] = _Path

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="ask", response_sink=sink))
    await _drain(sink)

    second = adapter.calls[1]["messages"]
    assert len(second) == 3
    assert second[0]["role"] == "user"
    assert second[1]["role"] == "assistant"
    assert second[1]["content"] == raw_emit
    assert second[2]["role"] == "user"
    assert second[2]["content"] == "<tool_response>\n/path/X\n</tool_response>"


@pytest.mark.asyncio
async def test_cascade_does_not_reinject_memory_context_per_iteration(tmp_path: Path):
    """[Memory context] block appears EXACTLY ONCE in the messages list —
    only on messages[0], never re-injected on subsequent iterations."""

    class _Echo(BaseModel):
        text: str
        async def run(self, ctx) -> str:
            return f"r:{self.text}"

    class _ScriptedAdapter:
        def __init__(self):
            self.calls: list[dict] = []

        async def stream_messages(self, **kw):
            idx = len(self.calls)
            self.calls.append({**kw, "messages": list(kw["messages"])} if "messages" in kw else kw)
            scripts = [
                '<tool_call>{"name":"_Echo","arguments":{"text":"a"}}</tool_call>',
                '<tool_call>{"name":"_Echo","arguments":{"text":"b"}}</tool_call>',
                '<tool_call>{"name":"Say","arguments":{"text":"done"}}</tool_call>',
            ]
            text = scripts[min(idx, len(scripts) - 1)]
            yield StreamChunk(text=text, done=False)
            yield StreamChunk(text="", done=True)

    adapter = _ScriptedAdapter()
    iv = _FakeInnerVoice("foo")
    disp = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    disp._tools_by_name["_Echo"] = _Echo

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    assert len(adapter.calls) == 3
    final_msgs = adapter.calls[-1]["messages"]
    occurrences = sum(
        1 for m in final_msgs if "[Memory context]" in m.get("content", "")
    )
    assert occurrences == 1
    assert "[Memory context]" in final_msgs[0]["content"]


@pytest.mark.asyncio
async def test_dispatcher_passes_available_skills_to_scaffolding_renderer(tmp_path: Path):
    """Dispatcher reads memory_root/skills/*.md filenames and passes the
    sorted stems as available_skills= to the scaffolding renderer."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "morning.md").write_text("---\nname: morning\n---\n")
    (skills_dir / "bedtime.md").write_text("---\nname: bedtime\n---\n")

    captured: list[dict] = []

    class _SpyRenderer:
        def __init__(self):
            self._real = PromptRenderer()

        def render(self, template_name: str, **ctx):
            captured.append({"template": template_name, "ctx": ctx})
            return self._real.render(template_name, **ctx)

        def render_blocks(self, template_name: str, **ctx):
            return self._real.render_blocks(template_name, **ctx)

    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=_SpyRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    scaffolding_calls = [c for c in captured if c["template"] == "scaffolding"]
    assert scaffolding_calls, "scaffolding template should have been rendered"
    assert scaffolding_calls[0]["ctx"]["available_skills"] == ["bedtime", "morning"]


@pytest.mark.asyncio
async def test_respond_no_cascade_when_only_none_returning_tools(tmp_path: Path):
    """Say (returns None) → no cascade → turn ends after one round."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    assert len(adapter.calls) == 1


# ----- Rolling cascade compact (2026-05-09) -----


@pytest.mark.asyncio
async def test_dispatcher_rolling_starts_empty(tmp_path: Path):
    """Fresh dispatcher: _rolling == []; first turn user message has no
    [Recent activity] block."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice("- foo")
    disp = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    assert disp._rolling == []

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    assert "[Recent activity]" not in user
    assert "[Memory context]" in user


@pytest.mark.asyncio
async def test_dispatcher_rolling_appends_after_each_turn(tmp_path: Path):
    """3 sequential turns -> _rolling has 3 entries in order."""

    class _ScriptedAdapter:
        def __init__(self):
            self.calls: list[dict] = []

        async def stream_messages(self, **kw):
            self.calls.append({**kw, "messages": list(kw["messages"])})
            yield StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            )
            yield StreamChunk(text="", done=True)

    adapter = _ScriptedAdapter()
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(compact_summaries=["s1", "s2", "s3"])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )

    for text in ("a", "b", "c"):
        sink: asyncio.Queue = asyncio.Queue()
        disp.dispatch(UserTextEvent(text=text, response_sink=sink))
        await _drain(sink)

    assert [s for _, s in disp._rolling] == ["s1", "s2", "s3"]


@pytest.mark.asyncio
async def test_dispatcher_subsequent_turn_includes_recent_activity_block(tmp_path: Path):
    """After 1 turn, the 2nd turn's first user message contains a
    [Recent activity] block listing prior summary, before [Memory context]."""

    class _ScriptedAdapter:
        def __init__(self):
            self.calls: list[dict] = []

        async def stream_messages(self, **kw):
            self.calls.append({**kw, "messages": list(kw["messages"])})
            yield StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            )
            yield StreamChunk(text="", done=True)

    adapter = _ScriptedAdapter()
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(compact_summaries=["summary 1", "summary 2"])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )

    for text in ("first", "second"):
        sink: asyncio.Queue = asyncio.Queue()
        disp.dispatch(UserTextEvent(text=text, response_sink=sink))
        await _drain(sink)

    second_user = adapter.calls[1]["messages"][0]["content"]
    assert "[Recent activity]\n" in second_user
    assert "summary 1" in second_user
    # And the [Recent activity] block precedes [Memory context].
    ra_idx = second_user.index("[Recent activity]")
    mc_idx = second_user.index("[Memory context]")
    assert ra_idx < mc_idx


@pytest.mark.asyncio
async def test_dispatcher_compact_called_with_full_cascade_messages(tmp_path: Path):
    """After a 2-iteration cascade (returning tool then Say), compact_cascade
    receives the full messages list (user, assistant, user(<tool_response>),
    assistant)."""

    class _Echo(BaseModel):
        text: str
        async def run(self, ctx) -> str:
            return f"e:{self.text}"

    class _ScriptedAdapter:
        def __init__(self):
            self.calls: list[dict] = []

        async def stream_messages(self, **kw):
            idx = len(self.calls)
            self.calls.append({**kw, "messages": list(kw["messages"])})
            scripts = [
                '<tool_call>{"name":"_Echo","arguments":{"text":"x"}}</tool_call>',
                '<tool_call>{"name":"Say","arguments":{"text":"done"}}</tool_call>',
            ]
            yield StreamChunk(text=scripts[min(idx, 1)], done=False)
            yield StreamChunk(text="", done=True)

    adapter = _ScriptedAdapter()
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(compact_summaries=["s"])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    disp._tools_by_name["_Echo"] = _Echo

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    assert len(inst.compact_calls) == 1
    call = inst.compact_calls[0]
    assert call["perception"] == "hi"
    msgs = call["cascade_messages"]
    # user(framed) + assistant(round1) + user(<tool_response>) + assistant(round2)
    assert len(msgs) == 4
    assert msgs[0]["role"] == "user"
    assert "[Message]" in msgs[0]["content"]
    assert msgs[1]["role"] == "assistant"
    assert msgs[2]["role"] == "user"
    assert "<tool_response>" in msgs[2]["content"]
    assert msgs[3]["role"] == "assistant"


@pytest.mark.asyncio
async def test_dispatcher_compact_runs_after_same_tool_abort(tmp_path: Path):
    """Same-tool 3-fail abort: compact_cascade still runs, summary still
    appends to _rolling."""

    class _RoundedAdapter:
        def __init__(self):
            self.calls: list[dict] = []

        async def stream_messages(self, **kw):
            self.calls.append({**kw, "messages": list(kw["messages"])})
            yield StreamChunk(
                text=(
                    '<tool_call>{"name":"InvokeSkill","arguments":'
                    '{"wrong":"x"}}</tool_call>'
                ),
                done=False,
            )
            yield StreamChunk(text="", done=True)

    adapter = _RoundedAdapter()
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(compact_summaries=["abort-summary"])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    items = await _drain(sink)

    assert any(
        isinstance(m, ErrorMsg) and "連續 3 次 InvokeSkill" in m.message
        for m in items
    )
    assert len(inst.compact_calls) == 1
    assert [s for _, s in disp._rolling] == ["abort-summary"]


@pytest.mark.asyncio
async def test_dispatcher_compact_failure_does_not_crash_turn(tmp_path: Path, caplog):
    """compact_cascade raising must not crash the turn: TurnEnd still
    fires, no extra ErrorMsg for compact, _rolling stays empty."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(compact_raises=RuntimeError("compact-boom"))
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    with caplog.at_level(logging.ERROR, logger="dollos.dispatcher"):
        disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
        items = await _drain(sink)

    # TurnEnd still arrives, plus the None sentinel.
    assert any(isinstance(m, TurnEnd) for m in items)
    # No ErrorMsg surfaced for compact failure (logged only).
    error_msgs = [m for m in items if isinstance(m, ErrorMsg)]
    assert not any("compact-boom" in m.message for m in error_msgs)
    # _rolling stays empty.
    assert disp._rolling == []
    # Failure was logged.
    assert any("compact_cascade" in r.message for r in caplog.records)


# ----- SubagentResultEvent (2026-05-09) -----


@pytest.mark.asyncio
async def test_dispatcher_handles_subagent_result_event(tmp_path: Path):
    """SubagentResultEvent flows through perceive/respond. The first user
    message body contains all four subagent fields (task / status /
    summary / details) so Doll can react to them."""
    from dollos.events import SubagentResultEvent

    captured_user_message: list[str] = []

    class _CaptureAdapter:
        def __init__(self):
            self.calls: list[dict] = []

        async def stream_messages(self, **kw):
            self.calls.append(
                {**kw, "messages": list(kw["messages"])}
                if "messages" in kw
                else kw
            )
            captured_user_message.append(kw["messages"][0]["content"])
            yield StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ack"}}</tool_call>',
                done=False,
            )
            yield StreamChunk(text="", done=True)

    adapter = _CaptureAdapter()
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=PromptRenderer(),
        identity=_doll_identity("x"),
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(
        SubagentResultEvent(
            subagent_id="abc1",
            task="search transcripts for coffee",
            status="ok",
            summary="found 3 hits",
            details="a.md, b.md, c.md",
            response_sink=sink,
        )
    )
    items = await _drain(sink)

    # The perception (which becomes [Message] body) lists all four fields.
    perception_body = captured_user_message[0]
    assert "你派出的 subagent 回來了" in perception_body
    assert "search transcripts for coffee" in perception_body
    assert "ok" in perception_body
    assert "found 3 hits" in perception_body
    assert "a.md, b.md, c.md" in perception_body
    # Doll responded with Say "ack" (we wired the adapter that way).
    assert any(isinstance(m, TextChunk) and m.text == "ack" for m in items)
    assert any(isinstance(m, TurnEnd) for m in items)


@pytest.mark.asyncio
async def test_subagent_result_event_uses_event_response_sink(tmp_path: Path):
    """_sink_of(SubagentResultEvent) returns event.response_sink."""
    from dollos.events import SubagentResultEvent

    sink: asyncio.Queue = asyncio.Queue()
    ev = SubagentResultEvent(
        subagent_id="x",
        task="t",
        status="ok",
        summary="s",
        details="d",
        response_sink=sink,
    )
    assert EventDispatcher._sink_of(ev) is sink


@pytest.mark.asyncio
async def test_dispatcher_passes_subagent_runner_into_tool_ctx(tmp_path: Path):
    """When a subagent_runner is wired, _respond's ToolCtx carries it
    through so SpawnSubagent.run() can dispatch new tasks."""

    captured: list[object] = []

    class _CaptureRunnerTool(BaseModel):
        async def run(self, ctx) -> None:
            # Side-effect capture; return None so cascade ends after this iter.
            captured.append(ctx.subagent_runner)

    class _FakeRunner:
        def __repr__(self) -> str:
            return "FAKE_RUNNER"

    runner = _FakeRunner()
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"_CaptureRunner","arguments":{}}</tool_call>',
                done=True,
            ),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=PromptRenderer(),
        identity=_doll_identity("x"),
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
        subagent_runner=runner,  # type: ignore[arg-type]
    )
    disp._tools_by_name["_CaptureRunner"] = _CaptureRunnerTool

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)
    # Tool ran exactly once and saw `runner` in ctx.
    assert captured == [runner]


# ----- Time awareness (2026-05-10) -----


@pytest.mark.asyncio
async def test_dispatcher_injects_now_block_in_first_user_message(tmp_path: Path):
    """First user message starts with [Now]\\n + ISO date + 週X 早上/上午/下午/晚上/深夜."""
    import re as _re

    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    disp = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    assert user.startswith("[Now]\n")
    # Date YYYY-MM-DD HH:MM:SS pattern
    assert _re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", user)
    # Chinese weekday + period descriptor
    assert _re.search(r"週[一二三四五六日]", user)
    assert any(p in user for p in ("深夜", "早上", "上午", "下午", "晚上"))
    # Order: [Now] before [Memory context] before [Message]
    assert user.index("[Now]") < user.index("[Memory context]") < user.index("[Message]")


@pytest.mark.asyncio
async def test_dispatcher_recent_activity_renders_with_seconds(tmp_path: Path):
    """[Recent activity] entries from today render with HH:MM:SS prefix."""
    from datetime import datetime as _dt
    import re as _re

    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    disp = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    today = _dt.now().replace(hour=14, minute=15, second=30, microsecond=0)
    disp._rolling = [(today, "主人查 pwd")]

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    assert "[Recent activity]" in user
    # Today's entry: HH:MM:SS prefix only (no date).
    assert _re.search(r"- 14:15:30 主人查 pwd", user)


@pytest.mark.asyncio
async def test_dispatcher_recent_activity_uses_full_date_for_old_entries(
    tmp_path: Path,
):
    """Yesterday's rolling entry renders with YYYY-MM-DD HH:MM:SS prefix."""
    from datetime import datetime as _dt, timedelta as _td

    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    disp = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    yesterday = _dt.now().replace(
        hour=10, minute=5, second=0, microsecond=0
    ) - _td(days=1)
    disp._rolling = [(yesterday, "old chat")]

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    assert "[Recent activity]" in user
    expected_prefix = f"- {yesterday:%Y-%m-%d %H:%M:%S} old chat"
    assert expected_prefix in user


# ----- Mood (2026-05-10) -----


@pytest.mark.asyncio
async def test_dispatcher_initial_mood_is_default(tmp_path: Path):
    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice()
    disp = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    assert disp._current_mood == "平靜，剛醒來"


@pytest.mark.asyncio
async def test_dispatcher_injects_mood_block(tmp_path: Path):
    """First user message includes [Mood]\\n<current mood> block."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    disp = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    assert "[Mood]\n平靜，剛醒來" in user


@pytest.mark.asyncio
async def test_dispatcher_mood_block_position(tmp_path: Path):
    """[Now] < [Mood] < [Recent activity] < [Memory context] < [Message]."""
    from datetime import datetime as _dt
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice("- foo")
    disp = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    today = _dt.now().replace(hour=12, minute=0, second=0, microsecond=0)
    disp._rolling = [(today, "did stuff")]

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    i_now = user.index("[Now]")
    i_mood = user.index("[Mood]")
    i_ra = user.index("[Recent activity]")
    i_mc = user.index("[Memory context]")
    i_msg = user.index("[Message]")
    assert i_now < i_mood < i_ra < i_mc < i_msg


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


@pytest.mark.asyncio
async def test_dispatcher_parses_mood_from_last_assistant_message(tmp_path: Path):
    """Big model writes MOOD: in <think>; dispatcher parses it from the
    last assistant message after cascade exits."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text=_think_with_mood("新心情"), done=False),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct()
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    assert disp._current_mood == "新心情"


@pytest.mark.asyncio
async def test_dispatcher_no_mood_update_when_assistant_lacks_mood_line(
    tmp_path: Path,
):
    """If last assistant message has no MOOD: line, current mood unchanged."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct()
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    assert disp._current_mood == "平靜，剛醒來"
    assert not (tmp_path / "mood").exists() or not list((tmp_path / "mood").iterdir())


@pytest.mark.asyncio
async def test_dispatcher_appends_mood_to_file_when_parsed(tmp_path: Path):
    import re as _re
    from datetime import date as _date
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text=_think_with_mood("新心情"), done=False),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct()
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    mood_file = tmp_path / "mood" / f"{_date.today():%Y-%m-%d}.md"
    assert mood_file.exists()
    body = mood_file.read_text()
    assert _re.search(r"## \(\d{2}:\d{2}:\d{2}\) 新心情", body)


@pytest.mark.asyncio
async def test_dispatcher_indexes_mood_file_with_memsearch(tmp_path: Path):
    from datetime import date as _date
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text=_think_with_mood("新心情"), done=False),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct()
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    mood_file = tmp_path / "mood" / f"{_date.today():%Y-%m-%d}.md"
    assert mood_file in ms.indexed


@pytest.mark.asyncio
async def test_dispatcher_mood_block_uses_updated_mood_in_subsequent_turn(
    tmp_path: Path,
):
    class _ScriptedAdapter:
        def __init__(self, emits: list[str]):
            self.calls: list[dict] = []
            self._emits = list(emits)

        async def stream_messages(self, **kw):
            self.calls.append({**kw, "messages": list(kw["messages"])})
            emit = self._emits.pop(0) if self._emits else ""
            yield StreamChunk(text=emit, done=False)
            yield StreamChunk(text="", done=True)

    adapter = _ScriptedAdapter([
        _think_with_mood("第一輪心情"),
        _think_with_mood("第二輪心情"),
    ])
    iv = _FakeInnerVoice()
    inst = _FakeInstinct()
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )

    for text in ("first", "second"):
        sink: asyncio.Queue = asyncio.Queue()
        disp.dispatch(UserTextEvent(text=text, response_sink=sink))
        await _drain(sink)

    second_user = adapter.calls[1]["messages"][0]["content"]
    assert "[Mood]\n第一輪心情" in second_user


@pytest.mark.asyncio
async def test_dispatcher_logs_cascade_iter_per_iter(tmp_path: Path):
    """Cascade with one failing tool then a successful Say should log_iter twice."""
    adapter = _FakeAdapter(
        chunks=[
            # Iter 1: emit a tool that doesn't exist -> failure -> cascade
            StreamChunk(
                text=(
                    "<think>\nSEEN: hi\nINTENT: try\nREVIEW: -\n"
                    "MOOD: ok\nTOOL: Bogus\n</think>\n"
                    '<tool_call>{"name":"Bogus","arguments":{}}</tool_call>'
                ),
                done=True,
            ),
        ]
    )
    # Second adapter response (after cascade) — must drain via re-call:
    # _FakeAdapter replays the same `chunks` each call, so the second call
    # also produces the bogus tool. Cascade aborts on consecutive 3 fails;
    # we just need >=2 iters logged. Run until natural termination.
    iv = _FakeInnerVoice("")
    fcl = _FakeCascadeLogger()
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=_FakeInstinct(),
        renderer=PromptRenderer(),
        identity=_doll_identity(),
        memory_root=tmp_path,
        memsearch=_FakeMemSearch(),
        transcripts_root=tmp_path / "transcripts",
        cascade_logger=fcl,
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    # At least 2 iters logged (cascade ran more than once).
    assert len(fcl.iters) >= 2
    # All iters share the same turn_id.
    turn_ids = {row["turn_id"] for row in fcl.iters}
    assert len(turn_ids) == 1
    # Iter numbers increment.
    assert [row["iter"] for row in fcl.iters[:2]] == [1, 2]


@pytest.mark.asyncio
async def test_dispatcher_log_iter_includes_parsed_think_and_tool_calls(tmp_path: Path):
    """log_iter receives assistant_text + tool_calls + results from the iter."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text=(
                    "<think>\nSEEN: greet\nINTENT: reply\nREVIEW: -\n"
                    "MOOD: 開心\nTOOL: Say\n</think>\n"
                    '<tool_call>{"name":"Say","arguments":{"text":"hi"}}</tool_call>'
                ),
                done=True,
            ),
        ]
    )
    iv = _FakeInnerVoice("")
    fcl = _FakeCascadeLogger()
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=_FakeInstinct(),
        renderer=PromptRenderer(),
        identity=_doll_identity(),
        memory_root=tmp_path,
        memsearch=_FakeMemSearch(),
        transcripts_root=tmp_path / "transcripts",
        cascade_logger=fcl,
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    assert len(fcl.iters) >= 1
    first = fcl.iters[0]
    assert first["turn_id"].startswith("fake-turn-")
    assert first["iter"] == 1
    assert "SEEN: greet" in first["assistant_text"]
    assert first["tool_calls"] == [
        {"name": "Say", "arguments": {"text": "hi"}}
    ]
    # Say returns None -> no ToolResult; results list is empty.
    assert first["results"] == []
    assert isinstance(first["duration_ms"], int)


# ----- Phase 1 schedule: new event perception -----


@pytest.mark.asyncio
async def test_dispatcher_perceives_scheduled_event(tmp_path: Path):
    from datetime import time as _time

    from dollos.events import ScheduledEvent

    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice("")
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(ScheduledEvent(
        entry_time=_time(7, 30),
        intent="morning ping",
        response_sink=sink,
    ))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    assert "07:30:00" in user
    assert "morning ping" in user


@pytest.mark.asyncio
async def test_dispatcher_perceives_daily_plan_event(tmp_path: Path):
    from dollos.events import DailyPlanEvent

    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice("")
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(DailyPlanEvent(response_sink=sink))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    assert "WriteSchedule" in user


# ----- Phase 1 schedule: serialization (gap #2) -----


@pytest.mark.asyncio
async def test_dispatcher_serializes_user_facing_events(tmp_path: Path):
    """Two UserTextEvents back-to-back: the second waits for the first."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"x"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ],
        delay=0.05,
    )
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink_a: asyncio.Queue = asyncio.Queue()
    sink_b: asyncio.Queue = asyncio.Queue()

    t0 = time.monotonic()
    dispatcher.dispatch(UserTextEvent(text="a", response_sink=sink_a))
    dispatcher.dispatch(UserTextEvent(text="b", response_sink=sink_b))

    items_a, items_b = await asyncio.gather(_drain(sink_a), _drain(sink_b))
    elapsed = time.monotonic() - t0

    assert any(isinstance(it, TextChunk) for it in items_a)
    assert any(isinstance(it, TextChunk) for it in items_b)
    # Serial ≈ 2 * delay; parallel ≈ 1 * delay.
    assert elapsed >= 0.09, f"elapsed {elapsed:.3f}s — looks parallel"


@pytest.mark.asyncio
async def test_dispatcher_does_not_serialize_subagent_result(tmp_path: Path):
    """gap #2: SubagentResultEvent runs in parallel even when a serialized
    cascade is active."""
    from dollos.events import SubagentResultEvent

    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"x"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ],
        delay=0.05,
    )
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink_a: asyncio.Queue = asyncio.Queue()
    sink_b: asyncio.Queue = asyncio.Queue()

    t0 = time.monotonic()
    dispatcher.dispatch(UserTextEvent(text="a", response_sink=sink_a))
    dispatcher.dispatch(SubagentResultEvent(
        subagent_id="x", task="t", status="ok",
        summary="s", details="d", response_sink=sink_b,
    ))

    items_a, items_b = await asyncio.gather(_drain(sink_a), _drain(sink_b))
    elapsed = time.monotonic() - t0

    assert any(isinstance(it, TextChunk) for it in items_a)
    assert any(isinstance(it, TextChunk) for it in items_b)
    assert elapsed < 0.09, f"elapsed {elapsed:.3f}s — subagent should run in parallel"


@pytest.mark.asyncio
async def test_dispatcher_processes_pending_after_cascade_ends(tmp_path: Path):
    """Pending events drain off the queue once the active cascade finishes."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"x"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ],
        delay=0.02,
    )
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink_a: asyncio.Queue = asyncio.Queue()
    sink_b: asyncio.Queue = asyncio.Queue()
    sink_c: asyncio.Queue = asyncio.Queue()

    dispatcher.dispatch(UserTextEvent(text="a", response_sink=sink_a))
    dispatcher.dispatch(UserTextEvent(text="b", response_sink=sink_b))
    dispatcher.dispatch(UserTextEvent(text="c", response_sink=sink_c))

    await _drain(sink_a)
    await _drain(sink_b)
    await _drain(sink_c)

    # After draining, the dispatcher pending queue is empty and no active cascade.
    assert dispatcher._pending == []
    assert (
        dispatcher._active_cascade is None or dispatcher._active_cascade.done()
    )


@pytest.mark.asyncio
async def test_dispatcher_injects_pending_block_into_iter_perception(tmp_path: Path):
    """If a UserTextEvent arrives while the active cascade is running, the
    NEXT iter inside that cascade should see a `[Pending events]` user
    message containing the queued event."""
    # Adapter yields two iterations: iter1 emits a NoteMemory tool (returns
    # nothing → cascade continues with tool_response). iter2 emits Say (no
    # cascade-worthy result → cascade ends).
    chunks_iter1 = [
        StreamChunk(
            text=(
                '<tool_call>{"name":"NotARealTool",'
                '"arguments":{}}</tool_call>'
            ),
            done=False,
        ),
        StreamChunk(text="", done=True),
    ]
    chunks_iter2 = [
        StreamChunk(
            text='<tool_call>{"name":"Say","arguments":{"text":"done"}}</tool_call>',
            done=False,
        ),
        StreamChunk(text="", done=True),
    ]

    @dataclass
    class _TwoIterAdapter(LLMAdapter):
        calls: list[dict] = field(default_factory=list)
        _iter: int = 0

        async def stream_completion(
            self, *, system, user, prefill="", stop=None,
            max_tokens=1024, tools=None, grammar=None,
        ):  # pragma: no cover
            yield StreamChunk(text="", done=True)

        async def stream_messages(
            self, *, system, messages, stop=None, max_tokens=1024,
            tools=None, grammar=None,
        ):
            self.calls.append({"messages": list(messages)})
            self._iter += 1
            chunks = chunks_iter1 if self._iter == 1 else chunks_iter2
            # Yield to the loop on iter1 so the test can dispatch B
            # between iter1 and iter2.
            if self._iter == 1:
                await asyncio.sleep(0.05)
            for c in chunks:
                yield c

    adapter = _TwoIterAdapter()
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    # Patch the adapter into the dispatcher.
    dispatcher._adapter = adapter

    sink_a: asyncio.Queue = asyncio.Queue()
    sink_b: asyncio.Queue = asyncio.Queue()

    dispatcher.dispatch(UserTextEvent(text="a", response_sink=sink_a))
    # Let iter1 start and reach its in-stream sleep, then queue B so that
    # iter2 (which follows the tool_response) sees the pending block.
    await asyncio.sleep(0.01)
    dispatcher.dispatch(UserTextEvent(text="b", response_sink=sink_b))

    await _drain(sink_a)
    await _drain(sink_b)

    # adapter.calls[0] = iter1 messages (1 user msg, no pending yet — B hadn't queued).
    # adapter.calls[1] = iter2 messages (after tool_response). Since B is queued
    # by then, the iter2 messages should include a `[Pending events]` user message.
    iter2_msgs = adapter.calls[1]["messages"]
    found_pending = any(
        msg["role"] == "user" and "[Pending events]" in msg["content"]
        and "主人說「b」" in msg["content"]
        for msg in iter2_msgs
    )
    assert found_pending, f"no [Pending events] in iter2 messages: {iter2_msgs}"


@pytest.mark.asyncio
async def test_dispatcher_no_pending_block_when_empty(tmp_path: Path):
    """With no other events queued, no `[Pending events]` block appears."""
    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice("")
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    assert "[Pending events]" not in user
