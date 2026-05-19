import os

import numpy as np
import pytest


def test_cosine_sim_identical_vectors():
    from dollos.voice_eval.wavlm_sim import _cosine_sim
    v = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    assert _cosine_sim(v, v) == pytest.approx(1.0)


def test_cosine_sim_orthogonal():
    from dollos.voice_eval.wavlm_sim import _cosine_sim
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0], dtype=np.float32)
    assert _cosine_sim(a, b) == pytest.approx(0.0, abs=1e-6)


def test_cosine_sim_zero_norm_returns_zero():
    from dollos.voice_eval.wavlm_sim import _cosine_sim
    z = np.zeros(3, dtype=np.float32)
    v = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    assert _cosine_sim(z, v) == 0.0
    assert _cosine_sim(v, z) == 0.0


@pytest.mark.skipif(
    not os.environ.get("VOICE_EVAL_HEAVY"),
    reason="heavy model download; set VOICE_EVAL_HEAVY=1 to enable",
)
def test_wavlm_sim_real_model(tmp_path):
    """End-to-end with real WavLM — gated."""
    import wave

    import numpy as np

    from dollos.voice_eval.wavlm_sim import WavLMSimRunner

    sr = 16000
    t = np.linspace(0, 2.0, sr * 2, endpoint=False)
    pcm = (np.sin(2 * np.pi * 200 * t) * 32767 * 0.5).astype(np.int16)
    wav_path = tmp_path / "sine.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())

    runner = WavLMSimRunner()
    score = runner.score(wav_path, wav_path)
    assert score == pytest.approx(1.0, abs=0.01)
