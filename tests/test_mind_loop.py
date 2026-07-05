"""Tests for MindLoop.iterate — voice_first cascade tests."""
from __future__ import annotations

import asyncio

import pytest

from dollos.ipc.messages import TextChunk
from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue


class _FakeLLM:
    """Yields the given text as a single streaming chunk on pass 1.

    Any re-feed pass (`stream_messages`, pass >= 2 of the sync-tool cascade)
    yields a terminal think-only stream with no speech and no tool call, so the
    cascade converges after exactly one re-feed. Single-pass behaviour for
    speech-only / fire-and-forget turns is unaffected.
    """

    def __init__(self, returns: str):
        self._returns = returns

    async def stream_completion(
        self, system, user, prefill, max_tokens=1024, grammar=None, purpose="cascade"
    ):
        class _Chunk:
            def __init__(self, text, done):
                self.text = text
                self.done = done

        yield _Chunk(text=self._returns, done=True)

    async def stream_messages(
        self, system, messages, max_tokens=1024, grammar=None,
        purpose="cascade", stop=None, tools=None,
    ):
        class _Chunk:
            def __init__(self, text, done):
                self.text = text
                self.done = done

        # Terminal pass: close think, emit nothing, no tool → cascade breaks.
        yield _Chunk(text="TOOL: none\n</think>\n\n", done=True)


def _drain_queue(q: asyncio.Queue) -> list:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


@pytest.mark.asyncio
async def test_iterate_streams_speech_to_sink_and_dispatches_tool(tmp_path):
    """Voice_first cascade: speak text → sink TextChunks; <tool_call> → dispatch."""
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}

    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, sink=sink, state=state)

    # Voice_first wire format: think emitted by grammar, then speak segments
    # interleaved with <tool_call> blocks.
    stream = (
        "SEEN: user said hi\n"
        "INTENT: greet\n"
        "REVIEW: ok\n"
        "MOOD: warm\n"
        "TOOL: NoteMemory\n"
        "</think>\n\n"
        "Hello there"
        "<tool_call>\n"
        '{"name":"NoteMemory","arguments":{"text":"user greeted"}}\n'
        "</tool_call>"
        " bye"
    )
    fake_llm = _FakeLLM(stream)
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=fake_llm,
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()

    # Sink received both speak segments as TextChunks
    chunks = _drain_queue(sink)
    text_chunks = [c for c in chunks if isinstance(c, TextChunk)]
    spoken = "".join(c.text for c in text_chunks)
    assert "Hello there" in spoken
    assert " bye" in spoken

    # NoteMemory tool ran → recent_outputs has a NoteMemory record
    assert state.iter_count == 1
    kinds = [o.kind for o in state.recent_outputs]
    assert "NoteMemory" in kinds
    # Also at least one Speech record from the streamed text
    assert "Speech" in kinds


@pytest.mark.asyncio
async def test_iterate_puts_turn_end_none_separator_on_sink(tmp_path):
    """A completed turn must end with a None turn-separator on the sink so the
    connection pump emits TurnEnd. Regression: turn_end never reached text/IPC
    clients because nothing produced the None the pump converts."""
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, sink=sink, state=state)

    # Speak-only turn (no tool_call) so the turn ends after one pass.
    stream = (
        "SEEN: user said hi\n"
        "INTENT: greet\n"
        "TOOL: none\n"
        "REVIEW: ok\n"
        "MOOD: warm\n"
        "</think>\n\n"
        "Hello there"
    )
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=_FakeLLM(stream),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()

    items = _drain_queue(sink)
    assert items, "sink received nothing"
    assert any(isinstance(c, TextChunk) for c in items)
    # The turn ended with exactly one trailing None separator.
    assert items[-1] is None, f"turn must end with a None separator; tail={items[-1]!r}"
    assert items.count(None) == 1, f"expected exactly one turn separator, got {items.count(None)}"


@pytest.mark.asyncio
async def test_iterate_sentence_chunks_speech_to_sink(tmp_path):
    """Multi-sentence LLM output should arrive as multiple TextChunks, one per sentence."""
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, sink=sink, state=state)

    stream = (
        "SEEN: hi\n"
        "INTENT: y\n"
        "REVIEW: z\n"
        "MOOD: q\n"
        "TOOL: speak\n"
        "</think>\n\n"
        "Hello there. How are you? Fine."
    )
    fake_llm = _FakeLLM(stream)
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=fake_llm,
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()

    chunks = _drain_queue(sink)
    text_chunks = [c for c in chunks if isinstance(c, TextChunk)]
    texts = [c.text for c in text_chunks]
    assert texts == ["Hello there. ", "How are you? ", "Fine."]


@pytest.mark.asyncio
async def test_iterate_persists_state(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="Awoke", t=1.0, data={}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)

    fake_llm = _FakeLLM(
        "SEEN: awoke\nINTENT: be\nREVIEW: ok\nMOOD: calm\nTOOL: none\n"
        "</think>\n\nhi"
    )
    persist_path = tmp_path / "mind_state.json"
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=fake_llm,
        system_prompt="SYS",
        state_persist_path=persist_path,
        tool_registry=tool_registry,
    )

    assert not persist_path.exists()
    await loop.iterate()
    assert persist_path.exists()
    import json
    data = json.loads(persist_path.read_text())
    assert data["iter_count"] == 1


@pytest.mark.asyncio
async def test_iterate_multiple_perceptions(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hello"}))
    queue.put(Perception(kind="UserSpoke", t=2.0, data={"text": "world"}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)

    fake_llm = _FakeLLM(
        "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: none\n</think>\n\nok"
    )
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=fake_llm,
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()
    assert len(state.recent_perceptions) == 2
    assert state.last_user_at == 2.0


@pytest.mark.asyncio
async def test_iterate_non_user_perception_no_user_at(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="Awoke", t=2.0, data={}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)

    fake_llm = _FakeLLM(
        "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: none\n</think>\n\nawoke"
    )
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=fake_llm,
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()
    assert state.iter_count == 1
    assert state.last_user_at == 0.0


@pytest.mark.asyncio
async def test_iterate_unknown_tool_skipped(tmp_path):
    """Tool call referencing a name not in registry is logged + skipped."""
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="Awoke", t=1.0, data={}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)

    stream = (
        "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: bogus\n</think>\n\n"
        "hi"
        "<tool_call>\n"
        '{"name":"DoesNotExist","arguments":{}}\n'
        "</tool_call>"
    )
    fake_llm = _FakeLLM(stream)
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=fake_llm,
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    # Should not crash
    await loop.iterate()
    assert state.iter_count == 1


@pytest.mark.asyncio
async def test_iterate_tool_validation_error_skipped(tmp_path):
    """Bad arguments for a known tool → ValidationError swallowed, loop continues."""
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="Awoke", t=1.0, data={}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)

    stream = (
        "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: NoteMemory\n</think>\n\n"
        "<tool_call>\n"
        '{"name":"NoteMemory","arguments":{"not_a_field":true}}\n'
        "</tool_call>"
    )
    fake_llm = _FakeLLM(stream)
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=fake_llm,
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()
    assert state.iter_count == 1


@pytest.mark.asyncio
async def test_shutdown_stops_run(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)

    fake_llm = _FakeLLM(
        "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: none\n</think>\n\nstopping"
    )
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=fake_llm,
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    loop.shutdown()
    await asyncio.wait_for(loop.run(), timeout=1.0)
    assert state.iter_count == 0


def _make_mind_loop(tmp_path=None, llm=None, queue=None, wal=None):
    """Build a MindLoop with sensible defaults for cancel tests."""
    import tempfile
    from pathlib import Path

    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp())
    state = MindState()
    if queue is None:
        queue = PerceptionQueue(wal=wal)
    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)
    if llm is None:
        llm = _FakeLLM(
            "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: none\n</think>\n\nhi"
        )
    return MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=llm,
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
        wal=wal,
    )


@pytest.mark.asyncio
async def test_is_cascade_active_false_when_idle(tmp_path):
    """No active cascade → is_cascade_active is False."""
    loop = _make_mind_loop(tmp_path)
    assert loop.is_cascade_active is False


@pytest.mark.asyncio
async def test_cancel_when_idle_is_noop(tmp_path):
    """Calling cancel_current_cascade with no active cascade doesn't raise."""
    loop = _make_mind_loop(tmp_path)
    loop.cancel_current_cascade()  # should not raise
    assert loop.is_cascade_active is False


class _SlowFakeLLM:
    """Yields many text chunks with delay between each."""

    def __init__(self, chunks: list[str], delay: float):
        self._chunks = chunks
        self._delay = delay
        self.consumed = 0

    async def stream_completion(
        self, system, user, prefill, max_tokens=1024, grammar=None, purpose="cascade"
    ):
        class _Chunk:
            def __init__(self, text, done):
                self.text = text
                self.done = done

        # Emit </think> first so the parser starts producing SpeakChunks
        yield _Chunk(text="</think>\n\n", done=False)
        self.consumed += 1
        for i, txt in enumerate(self._chunks):
            await asyncio.sleep(self._delay)
            yield _Chunk(text=txt, done=(i == len(self._chunks) - 1))
            self.consumed += 1


@pytest.mark.asyncio
async def test_cancel_mid_stream_exits_iterate_cleanly(tmp_path):
    """When cancel is set mid-stream, _llm_iterate returns within ~one chunk window."""
    chunks = [f"chunk{i} " for i in range(10)]
    slow_llm = _SlowFakeLLM(chunks, delay=0.1)
    loop = _make_mind_loop(tmp_path, llm=slow_llm)

    task = asyncio.create_task(loop._llm_iterate("prompt"))
    await asyncio.sleep(0.15)
    assert loop.is_cascade_active is True
    loop.cancel_current_cascade()
    await asyncio.wait_for(task, timeout=0.4)
    # After return, ctx is cleared
    assert loop.is_cascade_active is False
    # Not all 10 chunks consumed (proof of early exit)
    assert slow_llm.consumed < len(chunks) + 1


@pytest.mark.asyncio
async def test_iterate_truncates_wal_after_state_save(tmp_path):
    """After iterate() persists mind_state, WAL is truncated through last consumed seq."""
    from dollos.wal.perception_log import PerceptionWAL
    from dollos.mind.perception_queue import PerceptionQueue
    from dollos.mind.mind_state import Perception
    import time

    wal = PerceptionWAL(tmp_path / "wal.jsonl")
    queue = PerceptionQueue(wal=wal)
    queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"}))
    queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "bye"}))

    loop = _make_mind_loop(tmp_path, queue=queue, wal=wal)
    await loop.iterate()

    assert list(wal.iter_pending()) == []


@pytest.mark.asyncio
async def test_iterate_with_no_wal_unchanged(tmp_path):
    """iterate() with wal=None still works (backwards compat)."""
    from dollos.mind.mind_state import Perception
    loop = _make_mind_loop(tmp_path)
    # Put a perception so iterate() does real work
    loop._queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    await loop.iterate()


@pytest.mark.asyncio
async def test_iterate_does_not_truncate_wal_when_save_fails(tmp_path):
    """A failed save must NOT truncate the WAL — perceptions survive for replay."""
    import time
    from dollos.wal.perception_log import PerceptionWAL
    from dollos.mind.perception_queue import PerceptionQueue
    from dollos.mind.mind_state import Perception
    import dollos.mind.mind_loop as ml

    wal = PerceptionWAL(tmp_path / "wal.jsonl")
    queue = PerceptionQueue(wal=wal)
    queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"}))

    orig_save = ml.save_state
    ml.save_state = lambda *a, **k: False  # force failed save
    try:
        loop = _make_mind_loop(tmp_path, queue=queue, wal=wal)
        await loop.iterate()
    finally:
        ml.save_state = orig_save

    assert list(wal.iter_pending()) != []  # perceptions survive a failed save


# --- P2-capture (Task 5): REVIEW parsed into MindState.recent_reviews ---


@pytest.mark.asyncio
async def test_review_captured_into_state(tmp_path):
    """A turn whose think block carries a REVIEW line lands it in recent_reviews."""
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))

    stream = (
        "SEEN: x\n"
        "INTENT: y\n"
        "TOOL: none\n"
        "REVIEW: I should not have repeated myself\n"
        "MOOD: 平靜\n"
        "</think>\n\n"
        "好的"
    )
    loop = _make_mind_loop(tmp_path, llm=_FakeLLM(stream), queue=queue)
    await loop.iterate()

    reviews = list(loop._state.recent_reviews)
    assert reviews and "repeated myself" in reviews[-1]


@pytest.mark.asyncio
async def test_mood_think_line_not_written_to_state_mood(tmp_path):
    """The MOOD think line is parsed but MUST NOT overwrite state.mood (spec §6.2)."""
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))

    stream = (
        "SEEN: x\nINTENT: y\nTOOL: none\n"
        "REVIEW: noted\nMOOD: 暴怒\n</think>\n\nok"
    )
    loop = _make_mind_loop(tmp_path, llm=_FakeLLM(stream), queue=queue)
    default_emotion = loop._state.mood.emotion
    await loop.iterate()
    assert loop._state.mood.emotion == default_emotion  # unchanged by MOOD line


@pytest.mark.asyncio
async def test_recent_reviews_bounded_by_maxlen(tmp_path):
    """recent_reviews keeps at most maxlen entries (newest wins)."""
    loop = _make_mind_loop(tmp_path)
    for i in range(10):
        loop._state.recent_reviews.append(f"lesson {i}")
    assert len(loop._state.recent_reviews) <= 5
    assert "lesson 9" in list(loop._state.recent_reviews)


# --- P6.1 (Task 6): _dispatch_tool returns ToolResult incl. existence/args guard ---


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_failed_result(tmp_path):
    """An unknown tool name produces a typed failed ToolResult, not a silent no-op."""
    from dollos.cascade.tool_loop import ToolResult

    loop = _make_mind_loop(tmp_path)
    r = await loop._dispatch_tool("NoSuchTool", {})
    assert isinstance(r, ToolResult) and r.success is False
    # friendly message names the unknown tool
    assert "NoSuchTool" in r.detail


@pytest.mark.asyncio
async def test_dispatch_bad_args_returns_failed_result(tmp_path):
    """Args that fail pydantic validation produce a typed failed ToolResult."""
    from dollos.cascade.tool_loop import ToolResult

    loop = _make_mind_loop(tmp_path)
    r = await loop._dispatch_tool("Recall", {})  # missing required 'query'
    assert isinstance(r, ToolResult) and r.success is False
    # friendly message names the bad field
    assert "query" in r.detail


@pytest.mark.asyncio
async def test_dispatch_sync_tool_returns_success_result(tmp_path):
    """A sync tool that returns a str (NoteMemory) yields success=True with detail."""
    from dollos.cascade.tool_loop import ToolResult

    loop = _make_mind_loop(tmp_path)
    r = await loop._dispatch_tool("NoteMemory", {"text": "remember this"})
    assert isinstance(r, ToolResult) and r.success is True
    assert r.tool_name == "NoteMemory"
    assert r.detail  # non-empty confirmation string


@pytest.mark.asyncio
async def test_dispatch_tool_returning_none_yields_none(tmp_path):
    """A tool whose run() returns None (side-effect only) yields no ToolResult."""
    loop = _make_mind_loop(tmp_path)

    async def _none_run(self, ctx):
        return None

    recall_cls = loop._tool_registry["Recall"]
    orig_run = recall_cls.run
    recall_cls.run = _none_run
    try:
        r = await loop._dispatch_tool("Recall", {"query": "x"})
    finally:
        recall_cls.run = orig_run
    assert r is None


@pytest.mark.asyncio
async def test_dispatch_runtime_error_returns_failed_result(tmp_path):
    """A tool whose run() raises is turned into a failed ToolResult, not propagated."""
    from dollos.cascade.tool_loop import ToolResult

    loop = _make_mind_loop(tmp_path)

    async def _boom(self, ctx):
        raise RuntimeError("kaboom")

    # Monkeypatch the registered Recall tool's run to raise at call time.
    recall_cls = loop._tool_registry["Recall"]
    orig_run = recall_cls.run
    recall_cls.run = _boom
    try:
        r = await loop._dispatch_tool("Recall", {"query": "x"})
    finally:
        recall_cls.run = orig_run
    assert isinstance(r, ToolResult) and r.success is False
    assert "runtime" in r.detail.lower()


# --- P1 (Task 7): in-turn sync-tool re-feed on the streaming path + budget cap ---


class _ScriptedLLM:
    """Yields one scripted stream per LLM pass.

    Pass 1 is driven through ``stream_completion`` (the existing single-prompt
    path), passes >= 2 through ``stream_messages`` (the multi-message cascade
    path). Records the ``messages`` list seen by each ``stream_messages`` call
    and counts total passes so tests can assert single-pass vs multi-pass.
    """

    def __init__(self, scripts: list[str], before_pass=None):
        self._scripts = scripts
        self.pass_count = 0
        self.captured_messages: list[list[dict]] = []
        self.before_pass = before_pass

    def _script_for(self, idx: int) -> str:
        return self._scripts[idx] if idx < len(self._scripts) else self._scripts[-1]

    async def _emit(self, text):
        class _Chunk:
            def __init__(self, text, done):
                self.text = text
                self.done = done

        yield _Chunk(text=text, done=True)

    async def stream_completion(
        self, system, user, prefill="", max_tokens=1024, grammar=None,
        purpose="cascade",
    ):
        idx = self.pass_count
        self.pass_count += 1
        if self.before_pass is not None:
            self.before_pass(idx)
        async for c in self._emit(self._script_for(idx)):
            yield c

    async def stream_messages(
        self, system, messages, max_tokens=1024, grammar=None,
        purpose="cascade", stop=None, tools=None,
    ):
        idx = self.pass_count
        self.pass_count += 1
        self.captured_messages.append(list(messages))
        if self.before_pass is not None:
            self.before_pass(idx)
        async for c in self._emit(self._script_for(idx)):
            yield c


def _recall_pass(query: str = "coffee", speech: str = "查一下") -> str:
    return (
        "SEEN: x\nINTENT: y\nTOOL: Recall\nREVIEW: r\nMOOD: m\n</think>\n\n"
        f"{speech}"
        "<tool_call>\n"
        f'{{"name":"Recall","arguments":{{"query":"{query}"}}}}\n'
        "</tool_call>"
    )


def _speech_pass(speech: str) -> str:
    return (
        "SEEN: x\nINTENT: y\nTOOL: none\nREVIEW: r\nMOOD: m\n</think>\n\n"
        f"{speech}"
    )


def _shell_pass(speech: str = "跑個指令") -> str:
    return (
        "SEEN: x\nINTENT: y\nTOOL: Shell\nREVIEW: r\nMOOD: m\n</think>\n\n"
        f"{speech}"
        "<tool_call>\n"
        '{"name":"Shell","arguments":{"command":"echo hi"}}\n'
        "</tool_call>"
    )


def _note_memory_pass(text: str = "記下來", speech: str = "好") -> str:
    return (
        "SEEN: x\nINTENT: y\nTOOL: NoteMemory\nREVIEW: r\nMOOD: m\n</think>\n\n"
        f"{speech}"
        "<tool_call>\n"
        f'{{"name":"NoteMemory","arguments":{{"text":"{text}"}}}}\n'
        "</tool_call>"
    )


def _make_scripted_loop(tmp_path, scripts, sink=None, before_pass=None):
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, sink=sink, state=state)
    llm = _ScriptedLLM(scripts, before_pass=before_pass)
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=llm,
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )
    return loop, llm


@pytest.mark.asyncio
async def test_recall_result_refed_in_same_turn(tmp_path):
    """Pass 1 emits a Recall call → a 2nd pass runs whose input carries the
    Recall result as <tool_response>; both passes' speech reach the sink in order."""
    sink: asyncio.Queue = asyncio.Queue()
    loop, llm = _make_scripted_loop(
        tmp_path,
        scripts=[_recall_pass(speech="查一下"), _speech_pass("找到了")],
        sink=sink,
    )
    await loop.iterate()

    # A second pass ran via stream_messages, and its message list carried the
    # Recall result as a <tool_response> user message.
    assert llm.pass_count == 2
    assert llm.captured_messages, "stream_messages was never called"
    joined = "\n".join(
        m["content"] for m in llm.captured_messages[-1] if m["role"] == "user"
    )
    assert "<tool_response>" in joined

    # Both passes' speech reached the sink, in order.
    chunks = _drain_queue(sink)
    spoken = "".join(c.text for c in chunks if isinstance(c, TextChunk))
    assert "查一下" in spoken and "找到了" in spoken
    assert spoken.index("查一下") < spoken.index("找到了")


@pytest.mark.asyncio
async def test_fire_and_forget_tool_runs_single_pass(tmp_path):
    """A turn whose only tool is async Shell does NOT trigger a 2nd decode."""
    loop, llm = _make_scripted_loop(tmp_path, scripts=[_shell_pass()])
    await loop.iterate()
    assert llm.pass_count == 1


@pytest.mark.asyncio
async def test_note_memory_only_turn_single_pass(tmp_path):
    """A turn whose only tool is a SUCCESSFUL NoteMemory does NOT trigger a 2nd
    decode pass (spec §7 P1, finding 2).

    NoteMemory returns a "memory noted: …" confirmation Doll does not need to
    read to decide — re-feeding it would cost an extra full streaming decode on
    the project's #1-concern latency path. Only `Recall` (whose RESULT she must
    read) re-feeds on success."""
    loop, llm = _make_scripted_loop(tmp_path, scripts=[_note_memory_pass()])
    await loop.iterate()
    assert llm.pass_count == 1


@pytest.mark.asyncio
async def test_failing_tool_turn_still_cascades(tmp_path):
    """A tool FAILURE still re-feeds as a <tool_response> for a 2nd pass even
    though a NoteMemory SUCCESS no longer does — the allowlist gates SUCCESS
    only, so Doll is always told of (and can fix) her mistakes (spec §7 P1,
    finding 2)."""
    loop, llm = _make_scripted_loop(
        tmp_path,
        scripts=[_failing_tool_pass("Frobnicate"), _speech_pass("修好了")],
    )
    await loop.iterate()
    assert llm.pass_count == 2


@pytest.mark.asyncio
async def test_pure_speech_turn_single_pass(tmp_path):
    """A pure-speech turn (no tools) is a single pass identical to today."""
    loop, llm = _make_scripted_loop(tmp_path, scripts=[_speech_pass("你好")])
    await loop.iterate()
    assert llm.pass_count == 1


@pytest.mark.asyncio
async def test_cancel_during_second_pass_exits_clean(tmp_path):
    """Cancel between pass 1 and pass 2 → no exception, no pass-2 speech."""
    sink: asyncio.Queue = asyncio.Queue()

    holder: dict = {}

    def before_pass(idx):
        # When pass 2 (stream_messages) is about to stream, cancel the cascade
        # so the per-chunk cancel-check drops pass-2 speech cleanly.
        if idx >= 1 and "loop" in holder:
            holder["loop"].cancel_current_cascade()

    loop, llm = _make_scripted_loop(
        tmp_path,
        scripts=[_recall_pass(speech="查一下"), _speech_pass("SECOND")],
        sink=sink,
        before_pass=before_pass,
    )
    holder["loop"] = loop

    await loop.iterate()  # must not raise

    chunks = _drain_queue(sink)
    spoken = "".join(c.text for c in chunks if isinstance(c, TextChunk))
    assert "SECOND" not in spoken
    assert loop.is_cascade_active is False


@pytest.mark.asyncio
async def test_sync_tool_budget_cap_terminates(tmp_path):
    """A model that keeps emitting sync tools terminates at the budget cap."""
    from dollos.mind.mind_loop import MAX_SYNC_REFEED_PASSES

    # Every pass emits a Recall call; only the budget cap can stop the loop
    # (Recall always succeeds, so the 3-strike failure abort never fires).
    loop, llm = _make_scripted_loop(tmp_path, scripts=[_recall_pass()])
    await loop.iterate()
    assert llm.pass_count <= MAX_SYNC_REFEED_PASSES
    assert llm.pass_count == MAX_SYNC_REFEED_PASSES


# --- P6.3 (Task 8): bounded-severity read-only safe-mode ---


def _failing_tool_pass(tool: str = "Frobnicate") -> str:
    """A pass that emits a tool_call to an unknown tool → a failed ToolResult.

    Unknown tools are not fire-and-forget, so each one re-feeds and counts as a
    consecutive failure.
    """
    return (
        f"SEEN: x\nINTENT: y\nTOOL: {tool}\nREVIEW: r\nMOOD: m\n</think>\n\n"
        "嗯"
        "<tool_call>\n"
        f'{{"name":"{tool}","arguments":{{}}}}\n'
        "</tool_call>"
    )


@pytest.mark.asyncio
async def test_k_consecutive_failures_enter_safe_mode(tmp_path):
    """K=3 consecutive tool failures within a turn flip state.safe_mode."""
    loop, llm = _make_scripted_loop(tmp_path, scripts=[_failing_tool_pass()])
    await loop.iterate()
    assert loop._state.safe_mode is True
    assert loop._state.safe_mode_reason  # a non-empty reason is recorded


@pytest.mark.asyncio
async def test_rotating_invalid_calls_enter_safe_mode(tmp_path):
    """Distinct invalid tool names each pass (which the same-tool 3-strike
    misses) still trip the generic consecutive-failure threshold."""
    scripts = [
        _failing_tool_pass("Alpha"),
        _failing_tool_pass("Beta"),
        _failing_tool_pass("Gamma"),
        _failing_tool_pass("Delta"),
    ]
    loop, llm = _make_scripted_loop(tmp_path, scripts=scripts)
    await loop.iterate()
    assert loop._state.safe_mode is True


@pytest.mark.asyncio
async def test_safe_mode_excludes_write_tools(tmp_path):
    """_active_tool_registry() drops write/external tools while safe_mode is set."""
    loop = _make_mind_loop(tmp_path)
    loop._state.safe_mode = True
    tools = loop._active_tool_registry()
    names = set(tools)
    assert "Recall" in names
    assert "Shell" not in names
    assert "NoteMemory" not in names
    assert "SpawnWorkflow" not in names
    assert "WriteDiary" not in names
    assert "WriteSchedule" not in names


@pytest.mark.asyncio
async def test_active_registry_is_full_when_not_safe(tmp_path):
    """Outside safe mode the active registry is the full registry (no narrowing)."""
    loop = _make_mind_loop(tmp_path)
    assert loop._active_tool_registry() == loop._tool_registry


@pytest.mark.asyncio
async def test_safe_mode_entry_enqueues_help_perception(tmp_path):
    """Entering safe mode enqueues a SafeModeEntered perception (asks for help)."""
    loop, llm = _make_scripted_loop(tmp_path, scripts=[_failing_tool_pass()])
    await loop.iterate()
    assert loop._state.safe_mode is True
    percs = await loop._queue.drain()
    assert any(p.kind == "SafeModeEntered" for p in percs)


@pytest.mark.asyncio
async def test_user_turn_clears_safe_mode(tmp_path):
    """A successful user turn (pure speech) clears safe_mode at turn start."""
    loop = _make_mind_loop(tmp_path)
    loop._state.safe_mode = True
    loop._state.safe_mode_reason = "stuck"
    loop._queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    await loop.iterate()
    assert loop._state.safe_mode is False
    assert loop._state.safe_mode_reason == ""


@pytest.mark.asyncio
async def test_safe_mode_uses_reduced_grammar(tmp_path):
    """In safe mode the per-pass grammar is a non-None CONSTRAINED grammar built
    from the reduced registry — never None (which would be an unconstrained
    decode, a no-fallback violation; spec §8.3 / finding 1)."""
    loop = _make_mind_loop(tmp_path)
    full_grammar = loop._active_grammar()
    loop._state.safe_mode = True
    safe_grammar = loop._active_grammar()
    assert safe_grammar is not None
    assert "root ::=" in safe_grammar  # a real constrained GBNF, not None
    assert safe_grammar != full_grammar


@pytest.mark.asyncio
async def test_safe_mode_grammar_build_failure_propagates(tmp_path, monkeypatch):
    """A genuine safe-mode grammar build failure must SURFACE (raise), never be
    swallowed into grammar=None — which would let safe mode decode fully
    unconstrained (silent degradation, a no-fallback violation; spec §8.3 /
    finding 1). The reduced-tool grammar build is deterministic and must
    succeed; if it ever fails, that must halt loudly, not degrade silently."""
    loop = _make_mind_loop(tmp_path)
    loop._state.safe_mode = True
    loop._grammar_cache.clear()  # force a cache-miss so build is actually invoked

    def _boom(_tools):
        raise RuntimeError("grammar build broke")

    monkeypatch.setattr(
        "dollos.mind.mind_loop.build_voice_first_grammar", _boom
    )
    with pytest.raises(RuntimeError):
        loop._active_grammar()


# --- P6 (spec §13.1): deterministic successful-repeat detector wiring ---


@pytest.mark.asyncio
async def test_repeat_streak_enqueues_perception_at_threshold(tmp_path):
    """3 identical SetFocus-shaped outputs -> exactly one RepeatLoopDetected
    perception, enqueued after this turn's tool executions."""
    import time

    from dollos.mind.mind_state import OutputRecord

    loop, llm = _make_scripted_loop(tmp_path, scripts=[_speech_pass("")])
    for _ in range(3):
        loop._state.recent_outputs.append(
            OutputRecord(t=time.time(), kind="SetFocus", summary="focus → same thing")
        )

    await loop.iterate()

    percs = await loop._queue.drain()
    repeat_percs = [p for p in percs if p.kind == "RepeatLoopDetected"]
    assert len(repeat_percs) == 1
    assert repeat_percs[0].data == {
        "tool": "SetFocus", "summary": "focus → same thing", "count": 3,
    }
    assert loop._state.last_repeat_alert_key == "SetFocus:focus → same thing:3"


@pytest.mark.asyncio
async def test_repeat_streak_escalation_refires_with_updated_fingerprint(tmp_path):
    """A 4th identical output on a later turn re-fires (escalation 3 -> 4) with
    an updated fingerprint/count -- not suppressed as a duplicate alert."""
    import time

    from dollos.mind.mind_state import OutputRecord

    loop, llm = _make_scripted_loop(tmp_path, scripts=[_speech_pass("")])
    for _ in range(3):
        loop._state.recent_outputs.append(
            OutputRecord(t=time.time(), kind="SetFocus", summary="focus → same thing")
        )
    await loop.iterate()
    await loop._queue.drain()  # consume the first alert

    loop._queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "again"}))
    loop._state.recent_outputs.append(
        OutputRecord(t=time.time(), kind="SetFocus", summary="focus → same thing")
    )
    await loop.iterate()

    percs = await loop._queue.drain()
    repeat_percs = [p for p in percs if p.kind == "RepeatLoopDetected"]
    assert len(repeat_percs) == 1
    assert repeat_percs[0].data["count"] == 4
    assert loop._state.last_repeat_alert_key == "SetFocus:focus → same thing:4"


@pytest.mark.asyncio
async def test_repeat_streak_no_refire_without_a_new_streak(tmp_path):
    """After an alert, a differing action breaks the streak -> no further
    alert fires (the stored fingerprint is left untouched, per spec)."""
    import time

    from dollos.mind.mind_state import OutputRecord

    loop, llm = _make_scripted_loop(tmp_path, scripts=[_speech_pass("")])
    for _ in range(3):
        loop._state.recent_outputs.append(
            OutputRecord(t=time.time(), kind="SetFocus", summary="focus → same thing")
        )
    await loop.iterate()
    await loop._queue.drain()  # consume the first alert
    stored_fingerprint = loop._state.last_repeat_alert_key

    loop._queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "different"}))
    loop._state.recent_outputs.append(
        OutputRecord(t=time.time(), kind="Recall", summary="recalled: something else")
    )
    await loop.iterate()

    # No new perception is expected here, so drain with a short timeout
    # instead of blocking forever waiting for one that never arrives.
    percs = await loop._queue.drain(timeout_s=0.2)
    assert not any(p.kind == "RepeatLoopDetected" for p in percs)
    # Not proactively reset -- unchanged until a new genuine streak forms.
    assert loop._state.last_repeat_alert_key == stored_fingerprint


# --- P8 (spec 2026-07-01 persona-hardening §2): mechanical persona enforcement ---


@pytest.mark.asyncio
async def test_persona_violation_enqueues_perception(tmp_path):
    """A doll_text containing a banned substring enqueues exactly one
    PersonaDriftDetected perception with the violations list."""
    from dollos.character import Enforcement

    loop, llm = _make_scripted_loop(
        tmp_path, scripts=[_speech_pass("哈囉a~最近好嗎")]
    )
    loop._enforcement = Enforcement(banned_substrings=["a~"])

    await loop.iterate()

    percs = await loop._queue.drain()
    drift_percs = [p for p in percs if p.kind == "PersonaDriftDetected"]
    assert len(drift_percs) == 1
    assert drift_percs[0].data["snippet"] == "哈囉a~最近好嗎"
    assert len(drift_percs[0].data["violations"]) == 1


@pytest.mark.asyncio
async def test_persona_clean_turn_enqueues_nothing(tmp_path):
    """A clean turn (no violations) enqueues no PersonaDriftDetected."""
    from dollos.character import Enforcement

    loop, llm = _make_scripted_loop(
        tmp_path, scripts=[_speech_pass("哈囉最近好嗎")]
    )
    loop._enforcement = Enforcement(banned_substrings=["a~"])

    await loop.iterate()

    percs = await loop._queue.drain(timeout_s=0.2)
    assert not any(p.kind == "PersonaDriftDetected" for p in percs)


@pytest.mark.asyncio
async def test_persona_enforcement_default_never_fires(tmp_path):
    """MindLoop constructed with no `enforcement` kwarg never fires
    PersonaDriftDetected regardless of content -- zero-cost opt-out."""
    loop, llm = _make_scripted_loop(
        tmp_path, scripts=[_speech_pass("哈囉a~最近好嗎!!!!!!")]
    )

    await loop.iterate()

    percs = await loop._queue.drain(timeout_s=0.2)
    assert not any(p.kind == "PersonaDriftDetected" for p in percs)


class _CaptureLLM:
    """Captures the `user` prompt of pass 1; converges immediately."""

    def __init__(self) -> None:
        self.captured_user: str | None = None

    async def stream_completion(
        self, system, user, prefill, max_tokens=1024, grammar=None, purpose="cascade"
    ):
        self.captured_user = user

        class _Chunk:
            text = "TOOL: none\n</think>\n\n"
            done = True

        yield _Chunk()

    async def stream_messages(self, *a, **k):
        class _Chunk:
            text = ""
            done = True

        yield _Chunk()


@pytest.mark.asyncio
async def test_primary_language_threads_into_rendered_prompt(tmp_path):
    """MindLoop receives primary_language and threads it into render_mind, so the
    [Memory guideline] block appears in the per-turn prompt with the configured
    language interpolated (covers article-ingestion turns too)."""
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)

    llm = _CaptureLLM()
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=llm,
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
        primary_language="English",
    )

    await loop.iterate()

    assert llm.captured_user is not None
    assert "[Memory guideline]" in llm.captured_user
    assert "English" in llm.captured_user


@pytest.mark.asyncio
async def test_iterate_reflection_batch_sets_flag_and_injects_tool_outcomes(tmp_path):
    """MF-2: A batch drained through iterate() that contains a ReflectionMoment
    sets _is_reflection=True and injects [Tool outcomes] into the rendered prompt
    when tool_stats is non-empty.  A mixed [ReflectionMoment, UserSpoke] batch
    must also yield True (spec §6 requirement).

    Contrasting assertion: a plain UserSpoke-only batch leaves _is_reflection
    False and emits no [Tool outcomes] block.
    """
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}

    # --- Positive: mixed [ReflectionMoment, UserSpoke] batch ---
    state = MindState()
    state.tool_stats = {"Recall": {"ok": 5, "fail": 0}}  # non-empty so block renders
    queue = PerceptionQueue()
    queue.put(Perception(kind="ReflectionMoment", t=1.0, data={"iters_since_last": 10}))
    queue.put(Perception(kind="UserSpoke", t=1.1, data={"text": "reflect"}))
    ctx = _make_mind_ctx(tmp_path, state=state)
    llm = _CaptureLLM()
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=llm,
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )
    await loop.iterate()

    assert loop._is_reflection is True, "_is_reflection must be True for batch containing ReflectionMoment"
    assert llm.captured_user is not None, "_CaptureLLM must have captured the user prompt"
    assert "[Tool outcomes" in llm.captured_user, "[Tool outcomes] block must appear in reflection prompt"

    # --- Negative: plain UserSpoke batch leaves _is_reflection False + no block ---
    state2 = MindState()
    state2.tool_stats = {"Recall": {"ok": 5, "fail": 0}}
    queue2 = PerceptionQueue()
    queue2.put(Perception(kind="UserSpoke", t=2.0, data={"text": "hello"}))
    ctx2 = _make_mind_ctx(tmp_path, state=state2)
    llm2 = _CaptureLLM()
    loop2 = MindLoop(
        state=state2,
        queue=queue2,
        ctx=ctx2,
        llm=llm2,
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state2.json",
        tool_registry=tool_registry,
    )
    await loop2.iterate()

    assert loop2._is_reflection is False, "_is_reflection must remain False for UserSpoke-only batch"
    assert llm2.captured_user is not None
    assert "[Tool outcomes" not in llm2.captured_user, "[Tool outcomes] must not appear in non-reflection prompt"


def test_mind_loop_init_raises_on_unbuildable_grammar(tmp_path):
    """No-fallback: 一個帶未支援型別欄位的工具讓 grammar build 失敗時，
    MindLoop 必須在啟動時 raise，而不是靜默以 grammar=None 跑無約束 decode。"""
    from pydantic import BaseModel, Field
    from tests._dispatcher_helpers import _make_mind_ctx

    class _BadTool(BaseModel):
        flag: bool = Field(description="unsupported type")

    ctx = _make_mind_ctx(tmp_path)
    with pytest.raises(NotImplementedError):
        MindLoop(
            state=MindState(),
            queue=PerceptionQueue(),
            ctx=ctx,
            llm=_FakeLLM(""),
            system_prompt="",
            state_persist_path=tmp_path / "s.json",
            tool_registry={"_BadTool": _BadTool},
        )


@pytest.mark.asyncio
async def test_dispatch_tool_records_outcome(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    state = MindState()
    ctx = _make_mind_ctx(tmp_path, state=state)
    loop = MindLoop(state=state, queue=PerceptionQueue(), ctx=ctx, llm=_FakeLLM(""),
                    system_prompt="", state_persist_path=tmp_path / "s.json",
                    tool_registry={cls.__name__: cls for cls in __import__(
                        "dollos.tools", fromlist=["MAIN_TOOLS"]).MAIN_TOOLS})
    await loop._dispatch_tool("SetFocus", {"text": "x"})        # success
    await loop._dispatch_tool("NoSuchTool", {})                 # unknown → fail
    assert state.tool_stats["SetFocus"]["ok"] == 1
    assert state.tool_stats["NoSuchTool"]["fail"] == 1
    assert any(f.tool == "NoSuchTool" for f in state.recent_tool_failures)


@pytest.mark.asyncio
async def test_subagent_dispatch_does_not_record_to_doll(tmp_path):
    """Isolation: the subagent path (dispatch_tool_call) must NOT touch Doll's
    tool memory — recording lives only in the live wrapper."""
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.cascade.tool_loop import dispatch_tool_call
    ctx = _make_mind_ctx(tmp_path)
    await dispatch_tool_call({"name": "SetFocus", "arguments": {"text": "x"}}, ctx,
                             {"SetFocus": __import__("dollos.tools", fromlist=["SetFocus"]).SetFocus})
    assert ctx.mind_state.tool_stats == {}
    assert len(ctx.mind_state.recent_tool_failures) == 0


# --- I1 regression: turn-end None sentinel must fire even when render raises ---


@pytest.mark.asyncio
async def test_turn_end_none_emitted_when_render_mind_raises(tmp_path, monkeypatch):
    """I1 regression: if render_mind() raises, the turn-end None sentinel must
    still reach the sink so IPC clients don't block forever waiting for TurnEnd.

    Before the fix: the try/finally only wrapped _llm_iterate(); render_mind()
    was called outside it, so an exception there skipped the finally entirely.
    After the fix: the try/finally also wraps the render calls.
    """
    import dollos.mind.mind_loop as ml
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, sink=sink, state=state)

    def _boom_render(*a, **k):
        raise RuntimeError("render_mind exploded")

    monkeypatch.setattr(ml, "render_mind", _boom_render)

    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=_FakeLLM(""),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    # iterate() re-raises the render error — catch it so the test continues.
    with pytest.raises(RuntimeError, match="render_mind exploded"):
        await loop.iterate()

    items = _drain_queue(sink)
    assert None in items, (
        "turn-end None must be emitted even when render_mind() raises; "
        f"got sink items: {items!r}"
    )


@pytest.mark.asyncio
async def test_turn_end_none_emitted_when_render_tool_outcomes_raises(tmp_path, monkeypatch):
    """I1 regression: render_tool_outcomes() raising must also emit the turn-end
    None sentinel so the IPC client's turn boundary is preserved."""
    import dollos.mind.mind_loop as ml
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    # Trigger the reflection path so render_tool_outcomes is called.
    queue = PerceptionQueue()
    queue.put(Perception(kind="ReflectionMoment", t=1.0, data={}))
    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, sink=sink, state=state)

    def _boom_outcomes(*a, **k):
        raise RuntimeError("render_tool_outcomes exploded")

    monkeypatch.setattr(ml, "render_tool_outcomes", _boom_outcomes)

    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=_FakeLLM(""),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    with pytest.raises(RuntimeError, match="render_tool_outcomes exploded"):
        await loop.iterate()

    items = _drain_queue(sink)
    assert None in items, (
        "turn-end None must be emitted even when render_tool_outcomes() raises; "
        f"got sink items: {items!r}"
    )


def test_active_registry_reflection_includes_note_tool_lesson(tmp_path):
    """Reflection turns add NoteToolLesson; non-reflection excludes it; safe_mode wins."""
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.mind.mind_state import MindState
    from dollos.mind.perception_queue import PerceptionQueue
    from dollos.tools import MAIN_TOOLS
    state = MindState()
    loop = MindLoop(state=state, queue=PerceptionQueue(), ctx=_make_mind_ctx(tmp_path, state=state),
                    llm=_FakeLLM(""), system_prompt="", state_persist_path=tmp_path / "s.json",
                    tool_registry={c.__name__: c for c in MAIN_TOOLS})
    loop._is_reflection = False
    assert "NoteToolLesson" not in loop._active_tool_registry()
    loop._is_reflection = True
    assert "NoteToolLesson" in loop._active_tool_registry()
    # safe_mode wins over reflection
    state.safe_mode = True
    assert "NoteToolLesson" not in loop._active_tool_registry()


# --- Observability: CascadeLogger wired into live MindLoop ---


class _RecordingCascadeLogger:
    """Stub logger that records start_turn calls and log_iter kwargs."""

    FIXED_TURN_ID = "deadbeef"

    def __init__(self):
        self.log_iter_calls: list[dict] = []

    def start_turn(self) -> str:
        return self.FIXED_TURN_ID

    def log_iter(self, **kwargs) -> None:
        self.log_iter_calls.append(kwargs)


@pytest.mark.asyncio
async def test_cascade_logger_receives_tool_call_with_args(tmp_path):
    """CascadeLogger is called after each cascade pass with tool_calls containing
    the tool name and arguments — proving args are captured before dispatch.

    Also verifies:
    - assistant_text is non-empty (think block was captured)
    - duration_ms is an int
    - constructing MindLoop without cascade_logger still works (param is optional)
    """
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    # --- Positive: with cascade_logger wired ---
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, sink=sink, state=state)

    # Voice-first stream with a NoteMemory tool_call whose arg we can assert on.
    stream = (
        "SEEN: user said hi\n"
        "INTENT: greet\n"
        "REVIEW: ok\n"
        "MOOD: warm\n"
        "TOOL: NoteMemory\n"
        "</think>\n\n"
        "Hello"
        "<tool_call>\n"
        '{"name":"NoteMemory","arguments":{"text":"cascade-log-test-sentinel"}}\n'
        "</tool_call>"
    )

    recording_logger = _RecordingCascadeLogger()
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=_FakeLLM(stream),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
        cascade_logger=recording_logger,
    )

    await loop.iterate()

    # log_iter must have been called at least once
    assert recording_logger.log_iter_calls, "log_iter was never called"

    first_call = recording_logger.log_iter_calls[0]

    # turn_id matches what start_turn() returned
    assert first_call["turn_id"] == _RecordingCascadeLogger.FIXED_TURN_ID

    # tool_calls contains the NoteMemory entry with the correct argument
    assert first_call["tool_calls"], "tool_calls must be non-empty for a tool-call pass"
    tc = first_call["tool_calls"][0]
    assert tc["name"] == "NoteMemory"
    assert tc["arguments"].get("text") == "cascade-log-test-sentinel", (
        f"expected sentinel arg, got: {tc['arguments']!r}"
    )

    # assistant_text captured the think block
    assert first_call["assistant_text"], "assistant_text must be non-empty"

    # duration_ms is a non-negative int
    assert isinstance(first_call["duration_ms"], int)
    assert first_call["duration_ms"] >= 0

    # --- Negative: constructing without cascade_logger still works ---
    state2 = MindState()
    queue2 = PerceptionQueue()
    queue2.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    ctx2 = _make_mind_ctx(tmp_path, state=state2)
    loop2 = MindLoop(
        state=state2,
        queue=queue2,
        ctx=ctx2,
        llm=_FakeLLM("TOOL: none\n</think>\n\nok"),
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state2.json",
        tool_registry=tool_registry,
    )
    await loop2.iterate()  # must not raise
    assert loop2._state.iter_count == 1
