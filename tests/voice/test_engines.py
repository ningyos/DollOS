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
