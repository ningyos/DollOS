"""ABC + registry tests for voice engines."""
from __future__ import annotations

import pytest

from dollos.voice.engines import (
    ASR_REGISTRY,
    ASREngine,
    TTS_REGISTRY,
    TTSEngine,
    register_asr,
    register_tts,
)


def test_register_asr_adds_to_registry():
    @register_asr("fake-asr")
    class _FakeASR(ASREngine):
        async def transcribe(self, audio_pcm: bytes, sample_rate: int) -> str:
            return "ok"

        async def aclose(self) -> None:
            pass

    assert ASR_REGISTRY["fake-asr"] is _FakeASR
    del ASR_REGISTRY["fake-asr"]


def test_register_tts_adds_to_registry():
    @register_tts("fake-tts")
    class _FakeTTS(TTSEngine):
        sample_rate = 48000

        async def synthesize(self, text: str):
            yield b""

        async def aclose(self) -> None:
            pass

    assert TTS_REGISTRY["fake-tts"] is _FakeTTS
    del TTS_REGISTRY["fake-tts"]


def test_asr_abc_rejects_instantiation_without_methods():
    class _Bad(ASREngine):
        pass

    with pytest.raises(TypeError):
        _Bad()


def test_tts_abc_rejects_instantiation_without_methods():
    class _Bad(TTSEngine):
        pass

    with pytest.raises(TypeError):
        _Bad()


@pytest.mark.asyncio
async def test_fake_asr_transcribe_contract():
    @register_asr("contract-asr")
    class _C(ASREngine):
        async def transcribe(self, audio_pcm: bytes, sample_rate: int) -> str:
            assert isinstance(audio_pcm, bytes)
            assert isinstance(sample_rate, int)
            return "hello"

        async def aclose(self) -> None:
            pass

    eng = _C()
    out = await eng.transcribe(b"\x00\x00", 16000)
    assert out == "hello"
    await eng.aclose()
    del ASR_REGISTRY["contract-asr"]


@pytest.mark.asyncio
async def test_fake_tts_synthesize_yields_pcm():
    @register_tts("contract-tts")
    class _C(TTSEngine):
        sample_rate = 48000

        async def synthesize(self, text: str):
            yield b"\x00" * 100
            yield b"\x01" * 100

        async def aclose(self) -> None:
            pass

    eng = _C()
    chunks = [c async for c in eng.synthesize("hi")]
    assert chunks == [b"\x00" * 100, b"\x01" * 100]
    await eng.aclose()
    del TTS_REGISTRY["contract-tts"]
