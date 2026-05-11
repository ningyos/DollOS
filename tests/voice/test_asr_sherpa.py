"""SherpaOnnxASR tests.

Two layers:
- Unit tests (always run): model registry lookup, missing model handling.
- Integration tests (marked voice_integration): load a real SenseVoice
  int8 model from HuggingFace and transcribe a fixture utterance.
  Skipped by default; opt in via `pytest -m voice_integration`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dollos.voice.asr_sherpa import SHERPA_MODELS, SherpaOnnxASR


def test_sherpa_model_registry_has_known_models():
    assert "sense-voice-zh-en-ja-ko-yue" in SHERPA_MODELS
    entry = SHERPA_MODELS["sense-voice-zh-en-ja-ko-yue"]
    assert "hf_repo" in entry
    assert "files" in entry
    assert "loader" in entry
    assert entry["loader"] in {"sense_voice", "paraformer"}


def test_sherpa_unknown_model_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown sherpa-onnx model"):
        SherpaOnnxASR(model_id="does-not-exist", data_root=tmp_path)


def test_sherpa_explicit_model_dir_skips_download(tmp_path: Path):
    """If model_dir is set and incomplete, constructor must NOT try to download."""
    custom = tmp_path / "my_models"
    custom.mkdir()
    with pytest.raises(FileNotFoundError, match="model file"):
        SherpaOnnxASR(
            model_id="sense-voice-zh-en-ja-ko-yue",
            data_root=tmp_path,
            model_dir=custom,
        )


@pytest.mark.voice_integration
@pytest.mark.asyncio
async def test_sherpa_transcribe_sense_voice_int8(tmp_path: Path):
    """Live integration: downloads SenseVoice (~239 MB) + transcribes a synthetic
    silence fixture. Skipped unless -m voice_integration."""
    engine = SherpaOnnxASR(
        model_id="sense-voice-zh-en-ja-ko-yue",
        data_root=tmp_path,
    )
    silence = b"\x00\x00" * 16000  # 1 second
    out = await engine.transcribe(silence, 16000)
    assert isinstance(out, str)
    await engine.aclose()
