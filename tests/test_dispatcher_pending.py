"""Tests for EventDispatcher — pending events + serialization (step 21).

Covers: SERIALIZE_TYPES queuing, parallel tasks, pending-block injection,
drain after cascade ends, no pending block when empty.
"""

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dollos.events import UserTextEvent
from dollos.ipc.messages import TextChunk
from dollos.llm.adapter import LLMAdapter, StreamChunk

from tests._dispatcher_helpers import (
    _FakeAdapter,
    _drain,
    _make_dispatcher,
)


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
    dispatcher = _make_dispatcher(adapter=adapter, tmp_path=tmp_path)

    sink_a: asyncio.Queue = asyncio.Queue()
    sink_b: asyncio.Queue = asyncio.Queue()

    t0 = time.monotonic()
    dispatcher.dispatch(UserTextEvent(text="a", response_sink=sink_a))
    dispatcher.dispatch(SubagentResultEvent(
        subagent_id="x", task="t", status="ok",
        summary="s", details="d",
        details_output_id="fake-id", details_line_count=1,
        response_sink=sink_b,
    ))

    items_a, items_b = await asyncio.gather(_drain(sink_a), _drain(sink_b))
    elapsed = time.monotonic() - t0

    assert any(isinstance(it, TextChunk) for it in items_a)
    assert any(isinstance(it, TextChunk) for it in items_b)
    assert elapsed < 0.09, f"elapsed {elapsed:.3f}s — looks serial"


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
    dispatcher = _make_dispatcher(adapter=adapter, tmp_path=tmp_path)

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
    dispatcher = _make_dispatcher(adapter=adapter, tmp_path=tmp_path)

    sink_a: asyncio.Queue = asyncio.Queue()
    sink_b: asyncio.Queue = asyncio.Queue()

    t0 = time.monotonic()
    dispatcher.dispatch(UserTextEvent(text="a", response_sink=sink_a))
    dispatcher.dispatch(SubagentResultEvent(
        subagent_id="x", task="t", status="ok",
        summary="s", details="d",
        details_output_id="fake-id", details_line_count=1,
        response_sink=sink_b,
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
    dispatcher = _make_dispatcher(adapter=adapter, tmp_path=tmp_path)

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
    dispatcher = _make_dispatcher(adapter=adapter, tmp_path=tmp_path)

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
    dispatcher = _make_dispatcher(adapter=adapter, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    assert "[Pending events]" not in user
