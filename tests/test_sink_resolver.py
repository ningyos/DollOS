import asyncio
import pytest

from dollos.mind.sink_resolver import SinkResolver, DummySink


@pytest.mark.asyncio
async def test_no_sink_returns_dummy() -> None:
    resolver = SinkResolver()
    sink = resolver()
    assert isinstance(sink, DummySink)


@pytest.mark.asyncio
async def test_register_then_resolve_returns_sink() -> None:
    resolver = SinkResolver()
    real_sink = asyncio.Queue()
    handle = resolver.register(real_sink)
    sink = resolver()
    assert sink is real_sink
    resolver.unregister(handle)
    assert isinstance(resolver(), DummySink)


@pytest.mark.asyncio
async def test_most_recent_wins() -> None:
    resolver = SinkResolver()
    sink_a = asyncio.Queue()
    sink_b = asyncio.Queue()
    resolver.register(sink_a)
    resolver.register(sink_b)
    assert resolver() is sink_b


@pytest.mark.asyncio
async def test_dummy_sink_drops_messages_silently() -> None:
    dummy = DummySink()
    # put_nowait on dummy should not raise
    dummy.put_nowait({"type": "text_chunk", "text": "hi"})
