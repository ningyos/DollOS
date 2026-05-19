import numpy as np
import pytest


def test_to_semitones_a4_is_zero():
    from dollos.voice_eval.prosody import _to_semitones
    a4 = np.array([440.0])
    assert _to_semitones(a4)[0] == pytest.approx(0.0)


def test_to_semitones_a5_is_twelve():
    from dollos.voice_eval.prosody import _to_semitones
    a5 = np.array([880.0])
    assert _to_semitones(a5)[0] == pytest.approx(12.0, abs=1e-3)


def test_stats_handles_empty():
    from dollos.voice_eval.prosody import _stats
    assert _stats(np.array([])) is None


def test_stats_basic():
    from dollos.voice_eval.prosody import _stats
    s = _stats(np.array([0.0, 0.0, 0.0, 12.0, 12.0, 12.0]))
    assert s is not None
    mean, p10, p90, std = s
    assert mean == pytest.approx(6.0)
    # std should be > 0 for non-uniform
    assert std > 0


@pytest.mark.skipif(
    True,  # generating a real wav for librosa.pyin is expensive — gate manually
    reason="prosody integration runs against real audio; manual test only",
)
def test_score_identical_returns_zero(tmp_path):
    """If we pass the same file as ref and synth, distance should be 0."""
    import wave
    import numpy as np
    from dollos.voice_eval.prosody import ProsodyRunner

    sr = 16000
    t = np.linspace(0, 1.0, sr, endpoint=False)
    # 200 Hz sine with vibrato to produce voiced F0 detection
    pcm = (np.sin(2 * np.pi * 200 * t) * 32767 * 0.5).astype(np.int16)
    wav_path = tmp_path / "sine.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())

    runner = ProsodyRunner()
    score = runner.score(wav_path, wav_path)
    assert score == pytest.approx(0.0, abs=1e-6)
