import os

import pytest

from dollos.voice_eval.wer import _normalize_for_wer


def test_normalize_lowercases_and_strips_punct():
    a = _normalize_for_wer("Hello, World! 你好。")
    b = _normalize_for_wer("hello world 你好")
    assert a == b


def test_normalize_collapses_whitespace():
    a = _normalize_for_wer("  hello   world  ")
    b = _normalize_for_wer("hello world")
    assert a == b


def test_normalize_nfc_unicode():
    """Composed vs decomposed Unicode forms should normalize equal."""
    # é can be U+00E9 (composed) or U+0065 + U+0301 (decomposed)
    a = _normalize_for_wer("café")  # composed
    b = _normalize_for_wer("café")  # decomposed
    assert a == b


@pytest.mark.skipif(
    not os.environ.get("VOICE_EVAL_HEAVY"),
    reason="heavy model download; set VOICE_EVAL_HEAVY=1 to enable",
)
def test_wer_real_whisper(tmp_path):
    """End-to-end with faster-whisper — gated."""
    import wave

    import numpy as np

    from dollos.voice_eval.wer import WERRunner

    sr = 16000
    pcm = np.zeros(sr, dtype=np.int16)  # silence — will transcribe to nothing
    wav_path = tmp_path / "silence.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())

    runner = WERRunner()
    text = runner.transcribe(wav_path)
    assert isinstance(text, str)
