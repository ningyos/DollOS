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
    assert "unknown" in r.detail.lower()


@pytest.mark.asyncio
async def test_dispatch_bad_args_returns_failed_result(tmp_path):
    """Args that fail pydantic validation produce a typed failed ToolResult."""
    from dollos.cascade.tool_loop import ToolResult

    loop = _make_mind_loop(tmp_path)
    r = await loop._dispatch_tool("Recall", {})  # missing required 'query'
    assert isinstance(r, ToolResult) and r.success is False
    assert "validation" in r.detail.lower()


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
    assert "SpawnSubagent" not in names
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
    loop._safe_grammar = None

    def _boom(_tools):
        raise RuntimeError("grammar build broke")

    monkeypatch.setattr(
        "dollos.mind.mind_loop.build_voice_first_grammar", _boom
    )
    with pytest.raises(RuntimeError):
        loop._active_grammar()


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
