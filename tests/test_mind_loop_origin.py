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

from dollos.ipc.messages import TextChunk
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
