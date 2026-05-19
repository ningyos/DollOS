"""Tests for UTMOSv2 naturalness runner."""
from __future__ import annotations

import os
import sys

import pytest


def test_utmos_runner_import():
    """Module imports without crashing even if utmosv2 isn't installed."""
    from dollos.voice_eval.utmos import UTMOSRunner

    runner = UTMOSRunner()
    assert runner is not None


def test_utmos_unavailable_raises_clean_error():
    """If utmosv2 is NOT installed, _ensure_loaded raises RuntimeError with helpful msg."""
    from dollos.voice_eval.utmos import UTMOSRunner

    saved = sys.modules.get("utmosv2")
    sys.modules["utmosv2"] = None  # type: ignore[assignment]
    try:
        runner = UTMOSRunner()
        with pytest.raises(RuntimeError, match="not available"):
            runner._ensure_loaded()
    finally:
        if saved is not None:
            sys.modules["utmosv2"] = saved
        else:
            sys.modules.pop("utmosv2", None)


@pytest.mark.skipif(
    not os.environ.get("VOICE_EVAL_HEAVY"),
    reason="heavy model + maybe install issues; set VOICE_EVAL_HEAVY=1 to enable",
)
def test_utmos_real_score(tmp_path):
    """End-to-end if utmosv2 is installed."""
    import wave

    import numpy as np

    from dollos.voice_eval.utmos import UTMOSRunner

    sr = 16000
    t = np.linspace(0, 2.0, sr * 2, endpoint=False)
    pcm = (np.sin(2 * np.pi * 200 * t) * 32767 * 0.5).astype(np.int16)
    wav_path = tmp_path / "sine.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())

    runner = UTMOSRunner()
    score = runner.score(wav_path)
    assert 1.0 <= score <= 5.5
