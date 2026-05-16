"""Tests for EventDispatcher — basic cascade behavior.

Covers: tool dispatch, multi-iter cascade, stuck-tool abort, naked-text drop,
multi-tool ordering, validation errors, unknown tool, success-cascade,
rolling compact, message list structure.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel

from dollos.conversation_history import ConversationHistory
from dollos.scratchpad import Scratchpad
from dollos.dispatcher import EventDispatcher, ToolResult
from dollos.tool_outputs import ToolOutputStore
from dollos.events import RawEvent, UserTextEvent
from dollos.ipc.messages import ErrorMsg, TextChunk, TurnEnd
from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.prompts import PromptRenderer

from tests._dispatcher_helpers import (
    _FakeAdapter,
    _FakeCascadeLogger,
    _FakeInstinct,
    _FakeInnerVoice,
    _FakeMemSearch,
    _doll_identity,
    _drain,
    _make_dispatcher,
    _make_tool_ctx,
)


# ----- Basic dispatch lifecycle -----


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
    import time

    from dollos.llm.adapter import StreamChunk

    from tests._dispatcher_helpers import _HangAdapter

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


# ----- Tool dispatch / cascade mechanics -----


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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    assert len(adapter.calls) == 1
    from dollos.tools import MAIN_TOOLS
    assert adapter.calls[0].get("tools") == MAIN_TOOLS


# ----- _dispatch_tool_call unit tests -----


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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
    )
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_tool_ctx(sink, tmp_path, ms)

    fail = await disp._dispatch_tool_call({"name": 42, "arguments": {}}, ctx)

    assert isinstance(fail, ToolResult) and not fail.success
    assert "name" in fail.detail.lower()


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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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


# ----- Multi-round cascade tests -----


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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
            self.calls.append(
                {"system": system, "messages": list(messages), "tools": tools}
            )
            for c in self._chunks:
                yield c

    adapter = _OneShotAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"done"}}</tool_call>',
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    items = []
    while True:
        m = await sink.get()
        if m is None:
            break
        items.append(m)

    assert len(adapter.calls) == 1
    text_chunks = [m for m in items if isinstance(m, TextChunk)]
    assert any(m.text == "done" for m in text_chunks)
    assert any(isinstance(m, TurnEnd) for m in items)


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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    assert len(adapter.calls) == 1


# ----- Rolling cascade compact -----


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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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


# ----- Skills rendering -----


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
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
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
