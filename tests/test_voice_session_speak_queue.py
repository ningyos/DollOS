import asyncio
import pytest


class _FakeTTS:
    """Records each synthesize call + sleeps to simulate work."""
    sample_rate = 16000

    def __init__(self, delay_s: float = 0.1) -> None:
        self.calls: list[str] = []
        self.in_flight: list[str] = []
        self.max_concurrent: int = 0
        self._delay_s = delay_s

    async def synthesize(self, text: str):
        self.calls.append(text)
        self.in_flight.append(text)
        self.max_concurrent = max(self.max_concurrent, len(self.in_flight))
        await asyncio.sleep(self._delay_s)
        yield b""
        self.in_flight.remove(text)

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_speak_queue_serializes():
    """Enqueueing multiple texts in rapid succession runs strictly FIFO, not parallel."""
    from dollos.voice.session import VoiceSession

    tts = _FakeTTS(delay_s=0.05)
    session = VoiceSession.__new__(VoiceSession)
    session._tts = tts
    session._outbound_track = None
    session._is_open = True
    # Initialize the queue fields manually (bypassing __init__ for unit test)
    session._speak_queue = asyncio.Queue()
    session._speak_worker_task = None
    await session._start_speak_worker()

    try:
        await session.enqueue_speak("one")
        await session.enqueue_speak("two")
        await session.enqueue_speak("three")
        await session.wait_speak_idle(timeout_s=2.0)
    finally:
        await session._stop_speak_worker()

    assert tts.calls == ["one", "two", "three"]
    assert tts.max_concurrent == 1


@pytest.mark.asyncio
async def test_enqueue_speak_returns_immediately():
    """enqueue_speak doesn't block on TTS."""
    import time
    from dollos.voice.session import VoiceSession

    tts = _FakeTTS(delay_s=0.5)
    session = VoiceSession.__new__(VoiceSession)
    session._tts = tts
    session._outbound_track = None
    session._is_open = True
    session._speak_queue = asyncio.Queue()
    session._speak_worker_task = None
    await session._start_speak_worker()

    try:
        t0 = time.monotonic()
        await session.enqueue_speak("hello")
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, f"enqueue should return instantly, took {elapsed:.3f}s"
    finally:
        await session._stop_speak_worker()


@pytest.mark.asyncio
async def test_speak_worker_continues_after_error():
    """A failing synth doesn't kill the worker — next enqueue still processes."""
    from dollos.voice.session import VoiceSession

    class _FlakeyTTS:
        sample_rate = 16000
        def __init__(self):
            self.calls = []
        async def synthesize(self, text: str):
            self.calls.append(text)
            if text == "boom":
                raise RuntimeError("synthesize failed")
            yield b""
        async def aclose(self):
            pass

    tts = _FlakeyTTS()
    session = VoiceSession.__new__(VoiceSession)
    session._tts = tts
    session._outbound_track = None
    session._is_open = True
    session._speak_queue = asyncio.Queue()
    session._speak_worker_task = None
    await session._start_speak_worker()

    try:
        await session.enqueue_speak("first")
        await session.enqueue_speak("boom")
        await session.enqueue_speak("after")
        await session.wait_speak_idle(timeout_s=2.0)
    finally:
        await session._stop_speak_worker()

    assert tts.calls == ["first", "boom", "after"]
