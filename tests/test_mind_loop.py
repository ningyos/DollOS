"""Tests for MindLoop.iterate — voice_first cascade tests."""
from __future__ import annotations

import asyncio

import pytest

from dollos.ipc.messages import TextChunk
from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue


class _FakeLLM:
    """Yields the given text as a single streaming chunk."""

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
