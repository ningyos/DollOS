"""Tests for EventDispatcher — concurrent fan-out of RawEvents."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from dollos.dispatcher import EventDispatcher
from dollos.events import RawEvent, UserTextEvent
from dollos.ipc.messages import ErrorMsg, TextChunk, TurnEnd
from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.prompts import PromptRenderer

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
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append(
            {"system": system, "user": user, "prefill": prefill}
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


def _make_dispatcher(
    *,
    adapter: LLMAdapter,
    inner_voice: _FakeInnerVoice,
) -> EventDispatcher:
    return EventDispatcher(
        adapter=adapter,
        inner_voice=inner_voice,
        renderer=PromptRenderer(),
        character_profile="You are Doll.",
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
async def test_dispatch_is_sync_returns_immediately():
    adapter = _FakeAdapter(
        chunks=[StreamChunk(text="x"), StreamChunk(text="", done=True)],
        delay=0.05,
    )
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv)

    sink: asyncio.Queue = asyncio.Queue()
    ev = UserTextEvent(text="hi", response_sink=sink)

    # dispatch() must be sync (def, not async). Calling without await must
    # produce None, and the task must already be registered.
    result = dispatcher.dispatch(ev)
    assert result is None
    assert len(dispatcher._tasks) == 1

    # Drain to clean up.
    await _drain(sink)


@pytest.mark.asyncio
async def test_dispatch_pushes_chunks_then_turnend_then_none_sentinel():
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text="Hi"),
            StreamChunk(text=" there"),
            StreamChunk(text="", done=True),
        ],
    )
    iv = _FakeInnerVoice("RECALL:\n- foo\n")
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv)

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
async def test_recall_passes_perception_to_iv_and_to_adapter_user():
    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice("RECALL:\n- foo\n")
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(UserTextEvent(text="hello world", response_sink=sink))

    await _drain(sink)

    # IV.recall called with perception (= text in step-4 stub passthrough).
    assert iv.calls == ["hello world"]
    # adapter.user is perception; prefill is "{recall}DECISION: ".
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["user"] == "hello world"
    assert adapter.calls[0]["prefill"] == "RECALL:\n- foo\nDECISION: "


@pytest.mark.asyncio
async def test_handler_exception_pushes_errormsg_and_sentinel():
    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice(raises=RuntimeError("boom"))
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(UserTextEvent(text="x", response_sink=sink))

    items = await _drain(sink)
    assert len(items) == 2
    assert isinstance(items[0], ErrorMsg)
    assert "boom" in items[0].message
    assert items[1] is None


@pytest.mark.asyncio
async def test_perceive_typeerror_for_unsupported_raw_logged(caplog):
    """An unsupported RawEvent subclass: _sink_of raises TypeError; task dies
    with a logged exception. No sink to push to."""

    class FooEvent(RawEvent):
        pass

    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv)

    with caplog.at_level(logging.ERROR, logger="dollos.dispatcher"):
        dispatcher.dispatch(FooEvent())
        # Yield once so the task can run.
        for _ in range(5):
            await asyncio.sleep(0)
        # All tasks should have completed (no sink to drain).
        await asyncio.sleep(0.05)

    assert len(dispatcher._tasks) == 0
    # Either a 'no sink' or TypeError mention should be in the log.
    assert any(
        "no sink" in rec.message.lower() or "typeerror" in rec.message.lower()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_stop_cancels_in_flight_tasks():
    hang = _HangAdapter()
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=hang, inner_voice=iv)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(UserTextEvent(text="hang", response_sink=sink))

    # Wait for handler to enter the hanging stream_completion.
    await asyncio.wait_for(hang.entered.wait(), timeout=1.0)

    t0 = time.monotonic()
    await asyncio.wait_for(dispatcher.stop(), timeout=1.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5
    assert len(dispatcher._tasks) == 0


@pytest.mark.asyncio
async def test_dispatch_after_stop_raises_runtime_error():
    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv)

    await dispatcher.stop()

    sink: asyncio.Queue = asyncio.Queue()
    with pytest.raises(RuntimeError):
        dispatcher.dispatch(UserTextEvent(text="x", response_sink=sink))


@pytest.mark.asyncio
async def test_concurrent_dispatch_runs_in_parallel():
    adapter = _FakeAdapter(
        chunks=[StreamChunk(text="x"), StreamChunk(text="", done=True)],
        delay=0.05,
    )
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv)

    sink_a: asyncio.Queue = asyncio.Queue()
    sink_b: asyncio.Queue = asyncio.Queue()

    t0 = time.monotonic()
    dispatcher.dispatch(UserTextEvent(text="a", response_sink=sink_a))
    dispatcher.dispatch(UserTextEvent(text="b", response_sink=sink_b))

    items_a, items_b = await asyncio.gather(_drain(sink_a), _drain(sink_b))
    elapsed = time.monotonic() - t0

    # Both finished, both received chunks.
    assert any(isinstance(it, TextChunk) for it in items_a)
    assert any(isinstance(it, TextChunk) for it in items_b)
    # Parallel: total wall < 2 * single-call (~0.10s).
    assert elapsed < 0.09, f"elapsed {elapsed:.3f}s — looks serial"
