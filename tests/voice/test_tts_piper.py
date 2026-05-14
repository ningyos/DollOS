"""PiperVITSEngine tests with mocked piper package."""
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock
import pytest


@pytest.fixture
def fake_piper(monkeypatch):
    fake_module = MagicMock()
    fake_voice = MagicMock()
    fake_voice.config.sample_rate = 22050
    fake_voice.synthesize_stream_raw.return_value = iter([b"\x00\x01" * 500])
    fake_module.PiperVoice.load.return_value = fake_voice
    import sys
    monkeypatch.setitem(sys.modules, "piper", fake_module)
    return fake_module, fake_voice


async def test_piper_loads_and_yields_chunks(fake_piper, tmp_path):
    from dollos.voice.tts_piper import PiperVITSEngine
    onnx_path = tmp_path / "voice.onnx"
    config_path = tmp_path / "voice.onnx.json"
    onnx_path.write_bytes(b"fake"); config_path.write_text("{}")
    eng = PiperVITSEngine(voice_onnx_path=onnx_path, voice_config_path=config_path)
    assert eng.sample_rate == 22050
    chunks = [c async for c in eng.synthesize("hello")]
    assert len(chunks) > 0


async def test_piper_aclose_releases(fake_piper, tmp_path):
    from dollos.voice.tts_piper import PiperVITSEngine
    onnx_path = tmp_path / "voice.onnx"
    config_path = tmp_path / "voice.onnx.json"
    onnx_path.write_bytes(b"fake"); config_path.write_text("{}")
    eng = PiperVITSEngine(voice_onnx_path=onnx_path, voice_config_path=config_path)
    await eng.aclose()
