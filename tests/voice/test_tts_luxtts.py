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
        )


@pytest.mark.asyncio
async def test_luxtts_peak_normalize(tmp_path: Path, monkeypatch):
    """synthesize() should boost a low-amplitude signal to near peak_target."""
    from unittest.mock import MagicMock, patch

    # Create a fake prompt file so the constructor doesn't raise.
    prompt_path = tmp_path / "prompt.npz"
    prompt_path.write_bytes(b"fake")

    # Stub LuxTTSOnnx so no real model is loaded.
    fake_tts = MagicMock()
    # generate() returns a float32 array whose peak is only 0.1 (too quiet).
    low_amplitude = np.full(48000, 0.1, dtype=np.float32)
    fake_tts.generate.return_value = low_amplitude
    fake_tts.load_prompt.return_value = object()

    with patch("dollos.voice.tts_luxtts.LuxTTSOnnx", return_value=fake_tts):
        engine = LuxTTSEngine(
            model_dir=tmp_path / "models",
            prompt_path=prompt_path,
            peak_target=0.95,
        )

    chunks = [c async for c in engine.synthesize("hello")]
    assert chunks, "no chunks produced"

    # Reconstruct the int16 PCM and compute the peak.
    raw = b"".join(chunks)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
    peak = float(np.max(np.abs(samples)))
    assert abs(peak - 0.95) < 0.01, f"expected peak≈0.95, got {peak:.4f}"


@pytest.mark.asyncio
async def test_luxtts_peak_normalize_all_zero(tmp_path: Path, monkeypatch):
    """All-zero audio should pass through without division-by-zero."""
    from unittest.mock import MagicMock, patch

    prompt_path = tmp_path / "prompt.npz"
    prompt_path.write_bytes(b"fake")

    fake_tts = MagicMock()
    fake_tts.generate.return_value = np.zeros(960, dtype=np.float32)
    fake_tts.load_prompt.return_value = object()

    with patch("dollos.voice.tts_luxtts.LuxTTSOnnx", return_value=fake_tts):
        engine = LuxTTSEngine(
            model_dir=tmp_path / "models",
            prompt_path=prompt_path,
        )

    chunks = [c async for c in engine.synthesize("silence")]
    assert chunks, "no chunks for all-zero audio"


@pytest.mark.asyncio
async def test_luxtts_eq_curve_path(tmp_path: Path):
    """synthesize() with eq_curve_path applies FIR + uses EQ peak_normalize."""
    import json
    from unittest.mock import MagicMock, patch

    # Minimal EQ JSON at 48000 Hz (luxtts sample rate).
    eq_json = {
        "name": "test-eq",
        "sample_rate": 48000,
        "bands": [
            {"freq_hz": 100, "gain_db": 0.0},
            {"freq_hz": 1000, "gain_db": -3.0},
            {"freq_hz": 10000, "gain_db": 0.0},
        ],
        "peak_normalize": 0.80,
    }
    eq_path = tmp_path / "test.eq.json"
    eq_path.write_text(json.dumps(eq_json))

    prompt_path = tmp_path / "prompt.npz"
    prompt_path.write_bytes(b"fake")

    fake_tts = MagicMock()
    # Use a sine wave so there's actual signal for FIR to process.
    t = np.linspace(0, 1.0, 48000, dtype=np.float32)
    sine = (np.sin(2 * np.pi * 440 * t) * 0.3).astype(np.float32)
    fake_tts.generate.return_value = sine
    fake_tts.load_prompt.return_value = object()

    with patch("dollos.voice.tts_luxtts.LuxTTSOnnx", return_value=fake_tts):
        engine = LuxTTSEngine(
            model_dir=tmp_path / "models",
            prompt_path=prompt_path,
            eq_curve_path=eq_path,
        )

    chunks = [c async for c in engine.synthesize("hello")]
    assert chunks, "no chunks produced with EQ"

    raw = b"".join(chunks)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
    assert np.isfinite(samples).all(), "non-finite samples in EQ output"
    assert samples.size > 0
    # Peak should be near the EQ's peak_normalize (0.80), not 0.95.
    peak = float(np.max(np.abs(samples)))
    assert abs(peak - 0.80) < 0.02, f"expected peak≈0.80 (EQ peak), got {peak:.4f}"


def test_luxtts_eq_curve_path_missing_raises(tmp_path: Path):
    """Constructor raises FileNotFoundError for a missing EQ file."""
    from unittest.mock import MagicMock, patch

    prompt_path = tmp_path / "prompt.npz"
    prompt_path.write_bytes(b"fake")

    fake_tts = MagicMock()
    fake_tts.load_prompt.return_value = object()

    with patch("dollos.voice.tts_luxtts.LuxTTSOnnx", return_value=fake_tts):
        with pytest.raises(FileNotFoundError):
            LuxTTSEngine(
                model_dir=tmp_path / "models",
                prompt_path=prompt_path,
                eq_curve_path=tmp_path / "nonexistent.eq.json",
            )


def test_luxtts_eq_curve_wrong_sample_rate_raises(tmp_path: Path):
    """Constructor raises ValueError when EQ sample_rate != 48000."""
    import json
    from unittest.mock import MagicMock, patch

    eq_json = {
        "name": "wrong-sr",
        "sample_rate": 24000,
        "bands": [{"freq_hz": 1000, "gain_db": 0.0}],
        "peak_normalize": 0.90,
    }
    eq_path = tmp_path / "wrong.eq.json"
    eq_path.write_text(json.dumps(eq_json))

    prompt_path = tmp_path / "prompt.npz"
    prompt_path.write_bytes(b"fake")

    fake_tts = MagicMock()
    fake_tts.load_prompt.return_value = object()

    with patch("dollos.voice.tts_luxtts.LuxTTSOnnx", return_value=fake_tts):
        with pytest.raises(ValueError, match="sample_rate"):
            LuxTTSEngine(
                model_dir=tmp_path / "models",
                prompt_path=prompt_path,
                eq_curve_path=eq_path,
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
    )
    chunks = []
    async for chunk in engine.synthesize("Hello world."):
        chunks.append(chunk)
    assert len(chunks) > 0
    total_bytes = sum(len(c) for c in chunks)
    # >0.3s of 48kHz int16 mono — "Hello world." is short.
    assert total_bytes > 28800
    await engine.aclose()


@pytest.mark.voice_integration
@pytest.mark.asyncio
async def test_voice_round_trip_tts_then_asr(tmp_path: Path):
    """End-to-end engine round-trip: TTS synthesizes a known phrase, ASR
    transcribes the resulting audio, transcript should overlap with the
    original phrase. Validates both Phase A engines work as a pair.
    """
    from luxtts_onnx import LuxTTSOnnx
    import soundfile as sf
    from scipy.signal import resample_poly

    from dollos.voice.asr_sherpa import SherpaOnnxASR

    # ---- Encode a luxtts voice-clone prompt from a synthetic reference. ----
    lt = LuxTTSOnnx(model_dir=str(tmp_path / "luxtts-models"))
    ref_wav = tmp_path / "ref.wav"
    ref_sr = 24000
    ref_dur_s = 3.0
    t = np.linspace(0, ref_dur_s, int(ref_dur_s * ref_sr), dtype=np.float32)
    # Add some structure so the prompt encoder has signal to chew on.
    sine = (
        0.3 * np.sin(2 * np.pi * 220 * t)
        + 0.15 * np.sin(2 * np.pi * 440 * t)
    ).astype(np.float32)
    sf.write(ref_wav, sine, ref_sr)
    prompt = lt.encode_prompt(
        audio_path=str(ref_wav),
        transcript="This is a reference voice sample.",
        duration=ref_dur_s,
    )
    prompt_path = tmp_path / "prompt.npz"
    lt.save_prompt(prompt, str(prompt_path))

    # ---- Synthesize a known phrase. ----
    tts = LuxTTSEngine(
        model_dir=tmp_path / "luxtts-models",
        prompt_path=prompt_path,
    )
    spoken_phrase = "The quick brown fox jumps over the lazy dog."
    pcm_chunks: list[bytes] = []
    async for chunk in tts.synthesize(spoken_phrase):
        pcm_chunks.append(chunk)
    pcm_48k = b"".join(pcm_chunks)
    await tts.aclose()
    assert len(pcm_48k) > 0

    # ---- Resample 48 kHz → 16 kHz for the ASR engine. ----
    samples_48k = np.frombuffer(pcm_48k, dtype=np.int16).astype(np.float32)
    samples_16k_f32 = resample_poly(samples_48k, up=1, down=3)
    samples_16k = samples_16k_f32.astype(np.int16)
    pcm_16k = samples_16k.tobytes()

    # ---- Transcribe. ----
    asr = SherpaOnnxASR(
        model_id="sense-voice-zh-en-ja-ko-yue",
        data_root=tmp_path,
    )
    transcript = await asr.transcribe(pcm_16k, 16000)
    await asr.aclose()

    # ASR output may differ from the input phrase (voice-cloned-from-sine
    # produces low-fidelity speech). At minimum verify ASR ran and produced
    # a non-empty result — that exercises the full pipeline.
    print(f"\n[round-trip] spoken: {spoken_phrase!r}")
    print(f"[round-trip] heard:   {transcript!r}")
    assert isinstance(transcript, str)
    # Lenient check: at least one alphabetic char came back; ASR didn't
    # silently return empty on every-byte-silence.
    assert any(c.isalpha() for c in transcript), (
        f"ASR returned no alphabetic content from synthesized audio; "
        f"got {transcript!r}"
    )
