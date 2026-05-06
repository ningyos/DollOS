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

from dollos.dispatcher import EventDispatcher, ToolResult
from dollos.events import RawEvent, UserTextEvent
from dollos.ipc.messages import ErrorMsg, TextChunk, TurnEnd
from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.prompts import PromptRenderer
from dollos.tools import ToolCtx

# ----- Fakes -----


@dataclass
class _FakeAdapter(LLMAdapter):
    """Fake LLMAdapter — yields a configurable sequence of chunks.

    Captures call args for assertions.
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
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append(
            {"system": system, "user": user, "prefill": prefill, "tools": tools}
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
    ) -> AsyncIterator[StreamChunk]:
        self.entered.set()
        await asyncio.Event().wait()  # forever
        yield StreamChunk(text="", done=True)  # pragma: no cover


class _FakeInnerVoice:
    """Fake InnerVoice.recall — returns a fixed RECALL block, captures args."""

    def __init__(
        self,
        recall_text: str = "RECALL:\n- foo\n",
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
    """Fake Instinct.process — returns configurable summaries, captures calls."""

    def __init__(
        self,
        summaries: list[str] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._summaries = list(summaries) if summaries is not None else [""]
        self._raises = raises
        self.calls: list[str] = []

    async def process(self, event):  # type: ignore[no-untyped-def]
        self.calls.append(event.perception)
        if self._raises:
            raise self._raises
        if self._summaries:
            return self._summaries.pop(0)
        return ""


class _FakeMemSearch:
    def __init__(self) -> None:
        self.indexed: list = []

    async def index_file(self, path):
        self.indexed.append(path)


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
        character_profile="You are Doll.",
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
async def test_recall_passes_perception_to_iv_and_to_adapter_user(tmp_path: Path):
    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice("RECALL:\n- foo\n")
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(UserTextEvent(text="hello world", response_sink=sink))

    await _drain(sink)

    assert iv.calls == ["hello world"]
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["user"] == "hello world"
    assert adapter.calls[0]["prefill"] == "RECALL:\n- foo\nDECISION: "


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
    assert elapsed < 0.09, f"elapsed {elapsed:.3f}s — looks serial"


@pytest.mark.asyncio
async def test_dispatcher_calls_instinct_with_doll_event_perception(tmp_path: Path):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice(recall_text="RECALL:\n- foo\n")
    inst = _FakeInstinct(summaries=["主人剛打招呼。"])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=PromptRenderer(),
        character_profile="You are Doll.",
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

    assert inst.calls == ["hi"]


@pytest.mark.asyncio
async def test_dispatcher_prepends_state_block_when_summary_nonempty(tmp_path: Path):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice(recall_text="RECALL:\n- foo\n")
    inst = _FakeInstinct(summaries=["主人剛打招呼。"])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=PromptRenderer(),
        character_profile="You are Doll.",
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
    prefill = adapter.calls[0]["prefill"]
    assert prefill == "STATE:\n主人剛打招呼。\n\nRECALL:\n- foo\nDECISION: "


@pytest.mark.asyncio
async def test_dispatcher_skips_state_block_when_summary_empty(tmp_path: Path):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
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
        character_profile="You are Doll.",
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

    prefill = adapter.calls[0]["prefill"]
    assert "STATE:" not in prefill
    assert prefill == "RECALL:\n- foo\nDECISION: "


@pytest.mark.asyncio
async def test_dispatcher_instinct_error_surfaces_as_error_msg(tmp_path: Path):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice(recall_text="RECALL:\n- foo\n")
    inst = _FakeInstinct(raises=RuntimeError("instinct boom"))
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=PromptRenderer(),
        character_profile="You are Doll.",
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

    assert any(isinstance(m, ErrorMsg) and "instinct boom" in m.message for m in items)
    assert len(adapter.calls) == 0


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
        character_profile="You are Doll.",
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
        character_profile="You are Doll.",
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
        renderer=PromptRenderer(), character_profile="x",
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
        renderer=PromptRenderer(), character_profile="x",
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
        renderer=PromptRenderer(), character_profile="x",
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
        renderer=PromptRenderer(), character_profile="x",
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
        renderer=PromptRenderer(), character_profile="x",
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
        renderer=PromptRenderer(), character_profile="x",
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
        renderer=PromptRenderer(), character_profile="x",
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
        renderer=PromptRenderer(), character_profile="x",
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
        renderer=PromptRenderer(), character_profile="x",
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
        renderer=PromptRenderer(), character_profile="x",
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

        async def stream_completion(
            self, *, system, user, prefill, stop=None,
            max_tokens=1024, tools=None,
        ):
            idx = len(self.calls)
            self.calls.append(
                {"system": system, "user": user, "prefill": prefill,
                 "tools": tools}
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
        renderer=PromptRenderer(), character_profile="x",
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
    second_user = adapter.calls[1]["user"]
    assert "WhoKnows" in second_user
    assert "unknown" in second_user.lower()
    assert (
        "第 1 次" in second_user
        or "iteration 1" in second_user.lower()
        or "1 次重試" in second_user
    )
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

        async def stream_completion(
            self, *, system, user, prefill, stop=None,
            max_tokens=1024, tools=None,
        ):
            self.calls.append({"prefill": prefill})
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
        renderer=PromptRenderer(), character_profile="x",
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
async def test_respond_max_cascade_depth_emits_error_and_ends(tmp_path: Path, monkeypatch):
    from dollos import dispatcher as disp_mod
    monkeypatch.setattr(disp_mod, "MAX_CASCADE_DEPTH", 2)

    class _PerpetualFailAdapter:
        def __init__(self):
            self.calls = 0

        async def stream_completion(self, **kw):
            self.calls += 1
            yield StreamChunk(
                text='<tool_call>{"name":"WhoKnows","arguments":{}}</tool_call>',
                done=False,
            )
            yield StreamChunk(text="", done=True)

    adapter = _PerpetualFailAdapter()
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=["", "", "", ""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), character_profile="x",
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

    # cap=2 means iterations 1, 2 OK; iteration 3 -> error
    assert adapter.calls == 3
    assert any(
        isinstance(m, ErrorMsg) and "MAX_CASCADE_DEPTH" in m.message
        for m in items
    )
    assert any(isinstance(m, TurnEnd) for m in items)


@pytest.mark.asyncio
async def test_respond_cascade_perception_includes_multiple_fails(tmp_path: Path):
    """One round emits two fails; next round perception lists both."""

    class _TwoRoundAdapter:
        def __init__(self):
            self.calls = []

        async def stream_completion(self, **kw):
            self.calls.append(kw)
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
        renderer=PromptRenderer(), character_profile="x",
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    second_user = adapter.calls[1]["user"]
    assert "A" in second_user
    assert "B" in second_user


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
        renderer=PromptRenderer(), character_profile="x",
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
    assert "user] hi" in content
    assert "doll] ok" in content


@pytest.mark.asyncio
async def test_dispatcher_handles_diary_event(tmp_path: Path):
    """DiaryEvent flows through perceive/respond pipeline; perception
    tells Doll to write diary; daily.md ends up with diary section."""

    captured_user_message: list[str] = []

    class _CaptureAdapter:
        def __init__(self):
            self.calls = []

        async def stream_completion(self, **kw):
            self.calls.append(kw)
            captured_user_message.append(kw["user"])
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
        renderer=PromptRenderer(), character_profile="x",
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
