"""FishTTSEngine tests with mocked fish_tts package."""
from __future__ import annotations
import asyncio
from pathlib import Path
from unittest.mock import MagicMock
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


# ----- single-ref (backward compat) -----

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


# ----- multi-ref -----

async def test_fish_tts_engine_accepts_multi_ref(fake_fish_tts, tmp_path):
    from dollos.voice.tts_fish import FishTTSEngine
    paths = []
    for i in range(3):
        p = tmp_path / f"voice_{i}.npy"
        p.write_bytes(b"fake")
        paths.append(p)
    transcripts = ["clip one", "clip two", "clip three"]
    eng = FishTTSEngine(voice_profile_paths=paths, transcripts=transcripts)
    _, fake_synth = fake_fish_tts
    # set_references should have been called with a list of 3 profiles
    args, _ = fake_synth.set_references.call_args
    assert len(args[0]) == 3
    assert eng.sample_rate == 44100


async def test_fish_tts_engine_rejects_mismatched_lists(fake_fish_tts, tmp_path):
    from dollos.voice.tts_fish import FishTTSEngine
    p = tmp_path / "voice.npy"
    p.write_bytes(b"fake")
    with pytest.raises(ValueError, match="equal length"):
        FishTTSEngine(voice_profile_paths=[p], transcripts=["a", "b"])


async def test_fish_tts_engine_rejects_empty_paths(fake_fish_tts, tmp_path):
    from dollos.voice.tts_fish import FishTTSEngine
    with pytest.raises(ValueError, match="equal length"):
        FishTTSEngine(voice_profile_paths=[], transcripts=[])


async def test_fish_tts_engine_rejects_no_args(fake_fish_tts, tmp_path):
    from dollos.voice.tts_fish import FishTTSEngine
    with pytest.raises(ValueError, match="must provide either"):
        FishTTSEngine()


async def test_fish_tts_engine_rejects_single_path_without_transcript(fake_fish_tts, tmp_path):
    from dollos.voice.tts_fish import FishTTSEngine
    p = tmp_path / "voice.npy"
    p.write_bytes(b"fake")
    with pytest.raises(ValueError, match="requires transcript"):
        FishTTSEngine(voice_profile_path=p)


async def test_fish_tts_engine_multi_ref_rerefs_on_synthesize(fake_fish_tts, tmp_path):
    """synthesize() must re-call set_references so another char can't clobber us."""
    from dollos.voice.tts_fish import FishTTSEngine
    paths = [tmp_path / f"v{i}.npy" for i in range(2)]
    for p in paths:
        p.write_bytes(b"fake")
    eng = FishTTSEngine(voice_profile_paths=paths, transcripts=["a", "b"])
    _, fake_synth = fake_fish_tts
    fake_synth.reset_mock()
    _ = [c async for c in eng.synthesize("test")]
    # set_references called again inside synthesize
    fake_synth.set_references.assert_called_once()
    args, _ = fake_synth.set_references.call_args
    assert len(args[0]) == 2
