"""MindLoop routes each origin bucket's output to that origin's sink (§3.1 C1).

Two same-window external ChannelMessages from channels A and B must not
cross-deliver: A's reply lands on sink A only, B's on sink B only. Regression
target: before Task 4, `iterate()` drained the whole batch as one turn and
resolved a single sink via `sink_resolver()` (origin-less → most-recent
handle) — a mixed A/B batch would land BOTH replies on whichever sink was
registered last.
"""
from __future__ import annotations

import asyncio

import pytest

from dollos.ipc.channel_registry import ChannelRegistry
from dollos.ipc.messages import AddressedText, TextChunk
from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from dollos.tools import MAIN_TOOLS
from tests._dispatcher_helpers import _make_mind_ctx


class _Chunk:
    def __init__(self, text, done):
        self.text = text
        self.done = done


class _SeqLLM:
    """Yields a different fixed think+speech stream per call, in call order.

    Mirrors tests/test_mind_loop.py's `_FakeLLM` wire format (voice_first:
    think fields, `</think>`, then plain speech, `TOOL: none` so the turn
    converges after exactly one pass — no `stream_messages` re-feed call).
    Each bucket's `_run_one_turn` drives exactly one `stream_completion`
    call, so passing one stream per bucket (in drain_grouped's bucket order)
    lets the test assert per-origin content landed on the matching sink.
    """

    def __init__(self, returns: list[str]):
        self._returns = list(returns)
        self._i = 0

    async def stream_completion(
        self, system, user, prefill, max_tokens=1024, grammar=None, purpose="cascade"
    ):
        text = self._returns[self._i]
        self._i += 1
        yield _Chunk(text=text, done=True)

    async def stream_messages(
        self, system, messages, max_tokens=1024, grammar=None,
        purpose="cascade", stop=None, tools=None,
    ):
        # Not expected to be called (TOOL: none ⇒ single-pass turns), but
        # provide a terminal no-op stream for safety/symmetry with _FakeLLM.
        yield _Chunk(text="TOOL: none\n</think>\n\n", done=True)


def _drain_queue(q: asyncio.Queue) -> list:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def _speech_stream(seen: str, reply: str) -> str:
    return (
        f"SEEN: {seen}\n"
        "INTENT: greet\n"
        "TOOL: none\n"
        "REVIEW: ok\n"
        "MOOD: warm\n"
        "</think>\n\n"
        f"{reply}"
    )


@pytest.mark.asyncio
async def test_same_window_external_messages_do_not_cross_deliver(tmp_path):
    """A's message → sink A only; B's message → sink B only; no crosstalk."""
    state = MindState()
    queue = PerceptionQueue()
    # Same drain window: both enqueued before iterate() drains.
    queue.put(Perception(
        kind="ChannelMessage", t=1.0, data={"channel_id": "A", "text": "hi from A"},
    ))
    queue.put(Perception(
        kind="ChannelMessage", t=1.0, data={"channel_id": "B", "text": "hi from B"},
    ))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}

    # Existing harness (_make_mind_ctx) only registers ONE sink as
    # locus="internal" — extended here inline: build ctx with no sink, then
    # register two EXTERNAL sinks keyed by channel_id so sink_resolver(origin)
    # can route per-bucket.
    ctx = _make_mind_ctx(tmp_path, state=state)
    sink_a: asyncio.Queue = asyncio.Queue()
    sink_b: asyncio.Queue = asyncio.Queue()
    ctx.sink_resolver.register(sink_a, locus="external", channel_id="A")
    ctx.sink_resolver.register(sink_b, locus="external", channel_id="B")

    llm = _SeqLLM([
        _speech_stream("user A said hi", "reply-from-A"),
        _speech_stream("user B said hi", "reply-from-B"),
    ])

    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=llm,
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()

    items_a = _drain_queue(sink_a)
    items_b = _drain_queue(sink_b)

    text_a = "".join(c.text for c in items_a if isinstance(c, TextChunk))
    text_b = "".join(c.text for c in items_b if isinstance(c, TextChunk))

    assert "reply-from-A" in text_a
    assert "reply-from-B" not in text_a
    assert "reply-from-B" in text_b
    assert "reply-from-A" not in text_b

    # Each bucket's turn ends with its own None separator, on its own sink.
    assert items_a[-1] is None
    assert items_b[-1] is None

    # Both buckets ran (2 turns from 1 iterate() call — one per origin).
    assert state.iter_count == 2

    # current_origin resets to None after the batch completes.
    assert ctx.current_origin is None


@pytest.mark.asyncio
async def test_originless_batch_still_resolves_internal_sink(tmp_path):
    """Regression guard: the pre-existing internal single-sink path is
    unchanged — origin=None (no channel_id) still resolves to the
    most-recent INTERNAL sink, not a dummy or an external one."""
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}

    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, sink=sink, state=state)  # registered internal

    stream = _speech_stream("user said hi", "hello there")
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=_SeqLLM([stream]),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()

    items = _drain_queue(sink)
    text = "".join(c.text for c in items if isinstance(c, TextChunk))
    assert "hello there" in text
    assert items[-1] is None
    assert state.iter_count == 1


# --- AddressedText on external-origin turns (Task 5, spec §3.1) --------------


@pytest.mark.asyncio
async def test_external_origin_turn_emits_addressed_text(tmp_path):
    """A turn whose origin is registered 'external' in ChannelRegistry streams
    AddressedText(channel_id=origin), not TextChunk, so the bridge knows where
    to route it."""
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(
        kind="ChannelMessage", t=1.0, data={"channel_id": "A", "text": "hi from A"},
    ))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}

    ctx = _make_mind_ctx(tmp_path, state=state)
    sink_a: asyncio.Queue = asyncio.Queue()
    ctx.sink_resolver.register(sink_a, locus="external", channel_id="A")
    registry = ChannelRegistry()
    registry.register("A", locus="external", kind="discord")
    ctx.channel_registry = registry

    llm = _SeqLLM([_speech_stream("user A said hi", "reply-from-A")])

    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=llm,
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()

    items = _drain_queue(sink_a)
    addressed = [c for c in items if isinstance(c, AddressedText)]
    assert addressed, f"expected AddressedText on sink_a, got {items!r}"
    assert all(c.channel_id == "A" for c in addressed)
    text = "".join(c.text for c in addressed)
    assert "reply-from-A" in text
    # No plain TextChunk should have leaked onto the external sink.
    assert not any(isinstance(c, TextChunk) for c in items)


@pytest.mark.asyncio
async def test_internal_origin_turn_still_emits_text_chunk_with_registry_present(tmp_path):
    """Regression: even when a ChannelRegistry is wired, an origin-less
    (internal) turn must still emit TextChunk, not AddressedText."""
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}

    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, sink=sink, state=state)
    ctx.channel_registry = ChannelRegistry()  # present, but origin is None

    stream = _speech_stream("user said hi", "hello there")
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=_SeqLLM([stream]),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()

    items = _drain_queue(sink)
    text = "".join(c.text for c in items if isinstance(c, TextChunk))
    assert "hello there" in text
    assert not any(isinstance(c, AddressedText) for c in items)


# --- WAL truncation is batch-final, not per-bucket (Task 4 review fix) ---------


def _two_ext_sinks(ctx):
    sink_a: asyncio.Queue = asyncio.Queue()
    sink_b: asyncio.Queue = asyncio.Queue()
    ctx.sink_resolver.register(sink_a, locus="external", channel_id="A")
    ctx.sink_resolver.register(sink_b, locus="external", channel_id="B")
    return sink_a, sink_b


@pytest.mark.asyncio
async def test_interleaved_drain_truncates_wal_fully_once(tmp_path):
    """Interleaved A₁/B₁/A₂ in one drain window: after iterate() the WAL is
    fully consumed. Regression: per-bucket truncation would delete B₁ (seq 2)
    while processing bucket A (seqs 1,3), losing a channel's message."""
    from dollos.wal.perception_log import PerceptionWAL

    wal = PerceptionWAL(tmp_path / "wal.jsonl")
    state = MindState()
    queue = PerceptionQueue(wal=wal)
    # Global arrival order assigns seq 1,2,3; drain_grouped splits into
    # bucket A=[seq1, seq3], bucket B=[seq2].
    queue.put(Perception(kind="ChannelMessage", t=1.0, data={"channel_id": "A", "text": "a1"}))
    queue.put(Perception(kind="ChannelMessage", t=1.1, data={"channel_id": "B", "text": "b1"}))
    queue.put(Perception(kind="ChannelMessage", t=1.2, data={"channel_id": "A", "text": "a2"}))

    assert len(list(wal.iter_pending())) == 3  # all three logged before drain

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)
    _two_ext_sinks(ctx)

    llm = _SeqLLM([
        _speech_stream("a", "reply-A"),   # bucket A turn
        _speech_stream("b", "reply-B"),   # bucket B turn
    ])
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=llm,
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
        wal=wal,
    )

    await loop.iterate()

    # Every consumed perception is gone — including B₁ (seq 2) which a
    # per-bucket truncation through A's max seq (3) would have destroyed early.
    assert list(wal.iter_pending()) == []
    assert state.iter_count == 2


@pytest.mark.asyncio
async def test_wal_truncated_exactly_once_through_global_max_seq(tmp_path):
    """truncate_through must be called exactly ONCE, with the whole drain's
    global max seq — not once per bucket."""
    from dollos.wal.perception_log import PerceptionWAL

    wal = PerceptionWAL(tmp_path / "wal.jsonl")
    calls: list[int] = []
    orig = wal.truncate_through

    def _spy(seq: int) -> None:
        calls.append(seq)
        orig(seq)

    wal.truncate_through = _spy  # type: ignore[method-assign]

    state = MindState()
    queue = PerceptionQueue(wal=wal)
    # put() sets each Perception.seq in place (WAL append) — hold references so
    # we can read the assigned global max seq without touching queue internals.
    ps = [
        Perception(kind="ChannelMessage", t=1.0, data={"channel_id": "A", "text": "a1"}),
        Perception(kind="ChannelMessage", t=1.1, data={"channel_id": "B", "text": "b1"}),
        Perception(kind="ChannelMessage", t=1.2, data={"channel_id": "A", "text": "a2"}),
    ]
    for p in ps:
        queue.put(p)
    global_max = max(p.seq for p in ps)  # seq 3

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)
    _two_ext_sinks(ctx)

    llm = _SeqLLM([_speech_stream("a", "reply-A"), _speech_stream("b", "reply-B")])
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=llm,
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
        wal=wal,
    )

    await loop.iterate()

    assert calls == [global_max], (
        f"expected exactly one truncation through global max seq {global_max}, "
        f"got {calls}"
    )


@pytest.mark.asyncio
async def test_save_failure_in_any_bucket_skips_wal_truncation(tmp_path):
    """If any bucket's save_state fails, the batch-final truncation is skipped
    so ALL perceptions (both channels) survive for replay."""
    import dollos.mind.mind_loop as ml
    from dollos.wal.perception_log import PerceptionWAL

    wal = PerceptionWAL(tmp_path / "wal.jsonl")
    state = MindState()
    queue = PerceptionQueue(wal=wal)
    queue.put(Perception(kind="ChannelMessage", t=1.0, data={"channel_id": "A", "text": "a1"}))
    queue.put(Perception(kind="ChannelMessage", t=1.1, data={"channel_id": "B", "text": "b1"}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)
    _two_ext_sinks(ctx)

    llm = _SeqLLM([_speech_stream("a", "reply-A"), _speech_stream("b", "reply-B")])
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=llm,
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
        wal=wal,
    )

    orig_save = ml.save_state
    ml.save_state = lambda *a, **k: False  # every bucket's save fails
    try:
        await loop.iterate()
    finally:
        ml.save_state = orig_save

    # Neither channel's perception was truncated — both replay on next boot.
    assert len(list(wal.iter_pending())) == 2


class _CrashOnSecondLLM:
    """First turn streams normally; the second turn raises mid-stream. Used to
    simulate a crash AFTER the first bucket completes but BEFORE the second."""

    def __init__(self, first_stream: str):
        self._first = first_stream
        self._n = 0

    async def stream_completion(
        self, system, user, prefill, max_tokens=1024, grammar=None, purpose="cascade"
    ):
        self._n += 1
        if self._n == 1:
            yield _Chunk(text=self._first, done=True)
        else:
            raise RuntimeError("simulated crash during bucket B's turn")
            yield  # pragma: no cover

    async def stream_messages(self, system, messages, **kw):
        yield _Chunk(text="TOOL: none\n</think>\n\n", done=True)


@pytest.mark.asyncio
async def test_crash_in_second_bucket_preserves_first_and_second_wal(tmp_path):
    """The data-loss regression, directly: bucket A (seqs 1,3) completes, then
    bucket B (seq 2) crashes. B₁ must still be in the WAL for replay. Per-bucket
    truncation would have deleted B₁ (seq 2 <= A's max seq 3) before B ran,
    losing it forever; batch-final truncation never runs (the crash propagates
    before it), so ALL perceptions survive."""
    from dollos.wal.perception_log import PerceptionWAL

    wal = PerceptionWAL(tmp_path / "wal.jsonl")
    state = MindState()
    queue = PerceptionQueue(wal=wal)
    queue.put(Perception(kind="ChannelMessage", t=1.0, data={"channel_id": "A", "text": "a1"}))
    queue.put(Perception(kind="ChannelMessage", t=1.1, data={"channel_id": "B", "text": "b1"}))
    queue.put(Perception(kind="ChannelMessage", t=1.2, data={"channel_id": "A", "text": "a2"}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)
    _two_ext_sinks(ctx)

    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=_CrashOnSecondLLM(_speech_stream("a", "reply-A")),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
        wal=wal,
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        await loop.iterate()

    # All three perceptions still in the WAL — nothing was truncated because the
    # crash propagated before the batch-final truncation. Critically, B₁ (seq 2)
    # survives, which per-bucket truncation would have destroyed.
    remaining = {p.data.get("channel_id") for _, p in wal.iter_pending()}
    assert remaining == {"A", "B"}
    assert len(list(wal.iter_pending())) == 3
