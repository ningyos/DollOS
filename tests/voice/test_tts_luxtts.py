"""LuxTTSEngine tests."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from dollos.voice.tts_luxtts import LuxTTSEngine


def test_luxtts_sample_rate_is_48000():
    assert LuxTTSEngine.sample_rate == 48000


def test_luxtts_chunk_size_is_20ms_at_48k():
    from dollos.voice.tts_luxtts import _CHUNK_BYTES
    assert _CHUNK_BYTES == 1920


def test_luxtts_pcm_chunking_matches_array(tmp_path: Path):
    from dollos.voice.tts_luxtts import _pcm_chunks

    audio = np.zeros(48000, dtype=np.float32)
    audio[0] = 0.5
    chunks = list(_pcm_chunks(audio))
    assert len(chunks) == 50
    assert all(len(c) == 1920 for c in chunks)
    first_sample = int.from_bytes(chunks[0][:2], "little", signed=True)
    assert first_sample == 16383


def test_luxtts_prompt_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="prompt"):
        LuxTTSEngine(
            model_dir=tmp_path / "missing-models",
            prompt_path=tmp_path / "missing.npz",
            data_root=tmp_path,
        )


@pytest.mark.voice_integration
@pytest.mark.asyncio
async def test_luxtts_synthesize_yields_audio(tmp_path: Path):
    """Live integration: downloads luxtts models (~542 MB) and synthesizes
    a short utterance using a fixture-encoded prompt. Skipped unless
    -m voice_integration."""
    from luxtts_onnx import LuxTTSOnnx

    lt = LuxTTSOnnx(model_dir=str(tmp_path / "luxtts-models"))
    fake_wav = tmp_path / "ref.wav"
    sr = 24000
    t = np.linspace(0, 2.0, 2 * sr, dtype=np.float32)
    sine = (np.sin(2 * np.pi * 440 * t) * 0.3).astype(np.float32)
    import soundfile as sf
    sf.write(fake_wav, sine, sr)
    prompt = lt.encode_prompt(
        audio_path=str(fake_wav),
        transcript="A short test reference.",
        duration=2.0,
    )
    prompt_path = tmp_path / "prompt.npz"
    lt.save_prompt(prompt, str(prompt_path))

    engine = LuxTTSEngine(
        model_dir=tmp_path / "luxtts-models",
        prompt_path=prompt_path,
        data_root=tmp_path,
    )
    chunks = []
    async for chunk in engine.synthesize("Hello world."):
        chunks.append(chunk)
    assert len(chunks) > 0
    total_bytes = sum(len(c) for c in chunks)
    assert total_bytes > 48000  # >0.5s of 48kHz int16 mono
    await engine.aclose()
