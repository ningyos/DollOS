"""SileroVAD wrapper tests.

The model is loaded via onnxruntime on first call (auto-download from
HuggingFace into <data_root>/voice/vad/). Integration tests are marked
voice_integration; structural tests stand alone.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest


def test_silero_vad_chunk_size_constant():
    from dollos.voice.bridge.vad import SileroVAD
    # Silero v5 expects 512-sample chunks at 16 kHz (32 ms).
    assert SileroVAD.SAMPLES_PER_CHUNK == 512
    assert SileroVAD.SAMPLE_RATE == 16000


@pytest.mark.voice_integration
def test_silero_vad_runs_on_silence(tmp_path: Path):
    from dollos.voice.bridge.vad import SileroVAD

    vad = SileroVAD(data_root=tmp_path)
    # 32ms of silence
    silence = np.zeros(SileroVAD.SAMPLES_PER_CHUNK, dtype=np.float32)
    prob = vad.speech_probability(silence)
    assert 0.0 <= prob <= 1.0
    # Silence should have low speech probability
    assert prob < 0.3
    vad.close()


@pytest.mark.voice_integration
def test_silero_vad_runs_on_noise(tmp_path: Path):
    from dollos.voice.bridge.vad import SileroVAD

    vad = SileroVAD(data_root=tmp_path)
    rng = np.random.default_rng(0)
    # Loud noise → not necessarily speech, but pipeline should return a probability.
    noise = (rng.standard_normal(SileroVAD.SAMPLES_PER_CHUNK) * 0.3).astype(np.float32)
    prob = vad.speech_probability(noise)
    assert 0.0 <= prob <= 1.0
    vad.close()


def test_silero_vad_rejects_wrong_chunk_size(tmp_path: Path, monkeypatch):
    """Wrong chunk length should raise ValueError before any model run."""
    from dollos.voice.bridge import vad as vad_mod

    # Stub _ensure_model to no-op + skip session init.
    class _FakeSession:
        def run(self, *a, **kw): raise AssertionError("should not be called")

    monkeypatch.setattr(vad_mod, "_ensure_model", lambda data_root: tmp_path / "fake.onnx")
    monkeypatch.setattr(vad_mod.ort, "InferenceSession", lambda *a, **kw: _FakeSession())

    vad = vad_mod.SileroVAD(data_root=tmp_path)
    bad = np.zeros(100, dtype=np.float32)
    with pytest.raises(ValueError, match="chunk size"):
        vad.speech_probability(bad)
    vad.close()
