"""TTSObservingSink — Queue subclass that fires TTS on TextChunk put."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from dollos.ipc.messages import TextChunk, TurnEnd, ErrorMsg
from dollos.voice.sink import TTSObservingSink


@pytest.mark.asyncio
async def test_sink_fires_tts_on_text_chunk():
    session = MagicMock()
    # I7: code calls enqueue_speak, not speak — must mock the right method.
    session.enqueue_speak = AsyncMock()
    # I6: is_open must be truthy for the gate to pass.
    session.is_open = True
    sink = TTSObservingSink(voice_session_provider=lambda: session)
    sink.put_nowait(TextChunk(text="hello"))
    # Yield to let the scheduled task run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    session.enqueue_speak.assert_awaited_once_with("hello")


@pytest.mark.asyncio
async def test_sink_skips_tts_when_no_session():
    sink = TTSObservingSink(voice_session_provider=lambda: None)
    sink.put_nowait(TextChunk(text="hello"))
    # Should not crash; nothing else to assert structurally — just that
    # the put_nowait succeeded.
    item = await sink.get()
    assert isinstance(item, TextChunk)


@pytest.mark.asyncio
async def test_sink_skips_tts_when_session_not_open():
    """I6 regression: no task spawned when session.is_open is False."""
    session = MagicMock()
    session.enqueue_speak = AsyncMock()
    session.is_open = False  # session closed / closing
    sink = TTSObservingSink(voice_session_provider=lambda: session)
    sink.put_nowait(TextChunk(text="hello"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    session.enqueue_speak.assert_not_called()


@pytest.mark.asyncio
async def test_sink_passes_non_text_chunks_through():
    session = MagicMock()
    session.enqueue_speak = AsyncMock()
    session.is_open = True
    sink = TTSObservingSink(voice_session_provider=lambda: session)
    sink.put_nowait(TurnEnd())
    sink.put_nowait(ErrorMsg(message="x"))
    sink.put_nowait(None)
    items = [await sink.get() for _ in range(3)]
    assert isinstance(items[0], TurnEnd)
    assert isinstance(items[1], ErrorMsg)
    assert items[2] is None
    await asyncio.sleep(0)
    session.enqueue_speak.assert_not_called()


@pytest.mark.asyncio
async def test_sink_acts_as_normal_queue():
    sink = TTSObservingSink(voice_session_provider=lambda: None)
    sink.put_nowait(TextChunk(text="a"))
    sink.put_nowait(TextChunk(text="b"))
    a = await sink.get()
    b = await sink.get()
    assert a.text == "a"
    assert b.text == "b"
