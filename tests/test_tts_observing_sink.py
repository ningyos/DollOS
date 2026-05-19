# tests/test_tts_observing_sink.py
import asyncio
import pytest

from dollos.ipc.messages import TextChunk
from dollos.voice.sink import TTSObservingSink


class _MockSession:
    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.speak_called = False

    async def enqueue_speak(self, text: str) -> None:
        self.enqueued.append(text)

    async def speak(self, text: str) -> None:
        # Should NOT be called from sink anymore
        self.speak_called = True


@pytest.mark.asyncio
async def test_sink_calls_enqueue_speak():
    session = _MockSession()
    sink = TTSObservingSink(voice_session_provider=lambda: session)
    sink.put_nowait(TextChunk(text="hello"))
    sink.put_nowait(TextChunk(text="world"))
    # Let scheduled coros run
    await asyncio.sleep(0.05)
    assert session.enqueued == ["hello", "world"]
    assert session.speak_called is False


@pytest.mark.asyncio
async def test_sink_skips_when_no_session():
    sink = TTSObservingSink(voice_session_provider=lambda: None)
    # Should not raise
    sink.put_nowait(TextChunk(text="orphan"))
    await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_sink_passes_through_non_textchunk():
    session = _MockSession()
    sink = TTSObservingSink(voice_session_provider=lambda: session)
    # Any non-TextChunk should just queue without triggering enqueue_speak
    sink.put_nowait("not a text chunk")
    await asyncio.sleep(0.02)
    assert session.enqueued == []
    # And the item should still be in the queue
    item = await sink.get()
    assert item == "not a text chunk"
