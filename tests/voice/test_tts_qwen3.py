"""Qwen3TTSEngine tests with mocked qwen_tts package."""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import MagicMock
import numpy as np
import pytest


@pytest.fixture
def fake_qwen_tts(monkeypatch):
    fake_module = MagicMock()

    # Simulated waveform: 1 second of int16 silence at 24 kHz, returned as
    # (numpy_array_shape_(1,N), sample_rate).
    fake_wave = (np.zeros((1, 24000), dtype=np.int16),)
    fake_sr = 24000

    fake_model = MagicMock()
    fake_model.generate_voice_clone.return_value = (fake_wave, fake_sr)
    fake_module.Qwen3TTSModel.from_pretrained.return_value = fake_model

    monkeypatch.setitem(sys.modules, "qwen_tts", fake_module)

    # Reset module-level singleton so each test gets a fresh model load.
    import dollos.voice.tts_qwen3 as _mod
    monkeypatch.setattr(_mod, "_MODEL", None)
    monkeypatch.setattr(_mod, "_MODEL_ID", None)

    return fake_module, fake_model


async def test_qwen3_loads_model_and_holds_reference(fake_qwen_tts, tmp_path):
    from dollos.voice.tts_qwen3 import Qwen3TTSEngine
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"WAVE_FAKE")
    eng = Qwen3TTSEngine(
        model_id="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device="cuda:0",
        ref_audio=ref,
        ref_text="hi",
        language="English",
        instruction="excited",
    )
    fake_module, fake_model = fake_qwen_tts
    fake_module.Qwen3TTSModel.from_pretrained.assert_called_once()
    assert eng.sample_rate == 24000


async def test_qwen3_synthesize_yields_pcm_chunks(fake_qwen_tts, tmp_path):
    from dollos.voice.tts_qwen3 import Qwen3TTSEngine
    ref = tmp_path / "ref.wav"; ref.write_bytes(b"WAVE_FAKE")
    eng = Qwen3TTSEngine(
        model_id="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device="cuda:0",
        ref_audio=ref,
        ref_text="hi",
        language="English",
    )
    chunks = [c async for c in eng.synthesize("hello world")]
    assert chunks, "engine produced no chunks"
    # 20 ms at 24 kHz int16 = 480 samples = 960 bytes
    assert all(len(c) == 960 for c in chunks[:-1])
    # Total samples should match the fake 1s waveform
    assert sum(len(c) for c in chunks) >= 24000 * 2


async def test_qwen3_passes_instruction_prefix(fake_qwen_tts, tmp_path):
    """Engine prepends instruction to text input."""
    from dollos.voice.tts_qwen3 import Qwen3TTSEngine
    ref = tmp_path / "ref.wav"; ref.write_bytes(b"WAVE_FAKE")
    eng = Qwen3TTSEngine(
        model_id="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device="cuda:0",
        ref_audio=ref,
        ref_text="hi",
        language="English",
        instruction="excited, energetic",
    )
    async for _ in eng.synthesize("hello"):
        pass
    _, fake_model = fake_qwen_tts
    call = fake_model.generate_voice_clone.call_args
    # Implementation choice: either pass instruction as a separate kwarg if
    # supported, or prepend it to text. Test verifies one or the other.
    text_arg = call.kwargs.get("text") or call.args[0]
    has_instr_in_text = "excited, energetic" in text_arg
    has_instr_kw = call.kwargs.get("instruction") == "excited, energetic"
    assert has_instr_in_text or has_instr_kw, (
        f"instruction not passed; call.kwargs={call.kwargs}"
    )


async def test_qwen3_aclose(fake_qwen_tts, tmp_path):
    from dollos.voice.tts_qwen3 import Qwen3TTSEngine
    ref = tmp_path / "ref.wav"; ref.write_bytes(b"WAVE_FAKE")
    eng = Qwen3TTSEngine(
        model_id="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device="cuda:0",
        ref_audio=ref, ref_text="hi", language="English",
    )
    await eng.aclose()
