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
