"""FishTTSEngine tests with mocked fish_tts package."""
from __future__ import annotations
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


@pytest.fixture
def fake_fish_tts(monkeypatch):
    """Patch fish_tts module so import + get_instance work without torch."""
    fake_module = MagicMock()
    fake_synth = MagicMock()
    fake_synth.synthesize_stream.return_value = iter([b"\x00\x01" * 1000])
    fake_module.get_instance.return_value = fake_synth
    fake_profile = MagicMock()
    fake_module.VoiceProfile.load.return_value = fake_profile
    import sys
    monkeypatch.setitem(sys.modules, "fish_tts", fake_module)
    return fake_module, fake_synth


async def test_fish_engine_loads_singleton_and_sets_reference(fake_fish_tts, tmp_path):
    from dollos.voice.tts_fish import FishTTSEngine
    voice_path = tmp_path / "voice.npy"
    voice_path.write_bytes(b"fake")
    eng = FishTTSEngine(voice_profile_path=voice_path, transcript="hi")
    _, fake_synth = fake_fish_tts
    fake_synth.set_references.assert_called_once()
    assert eng.sample_rate == 44100


async def test_fish_engine_streams_pcm_chunks(fake_fish_tts, tmp_path):
    from dollos.voice.tts_fish import FishTTSEngine
    voice_path = tmp_path / "voice.npy"
    voice_path.write_bytes(b"fake")
    eng = FishTTSEngine(voice_profile_path=voice_path, transcript="hi")
    chunks = [c async for c in eng.synthesize("hello")]
    assert len(chunks) > 0
    total = b"".join(chunks)
    # Each chunk should be 20ms at 44.1k int16 = 1764 bytes
    assert all(len(c) == 1764 for c in chunks[:-1])  # last may be partial


async def test_fish_engine_aclose_releases(fake_fish_tts, tmp_path):
    from dollos.voice.tts_fish import FishTTSEngine
    voice_path = tmp_path / "voice.npy"
    voice_path.write_bytes(b"fake")
    eng = FishTTSEngine(voice_profile_path=voice_path, transcript="hi")
    await eng.aclose()
