"""Voice config loading from character packs."""
from __future__ import annotations

from pathlib import Path

import pytest

from dollos.voice.pack import (
    VoiceConfig,
    load_voice_config,
    no_voice_config,
)


def _write_pack(pack_dir: Path, *, with_voice: bool = True) -> None:
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "doll.toml").write_text(
        '[meta]\nid = "test"\nname = "Test"\n[identity]\nself=""\npersonality=""\ntaboos=""\n'
    )
    if with_voice:
        voice_dir = pack_dir / "voice"
        voice_dir.mkdir()
        (voice_dir / "engine.toml").write_text(
            """
[asr]
engine = "sherpa-onnx"
model_id = "sense-voice-zh-en-ja-ko-yue"
device = "cpu"

[tts]
engine = "luxtts-onnx"
prompt = "voice/luxtts/prompt.npz"
device = "cpu"
num_steps = 8
t_shift = 0.9
guidance_scale = 3.0
"""
        )


def test_load_voice_config_present(tmp_path: Path):
    pack_dir = tmp_path / "pack"
    _write_pack(pack_dir, with_voice=True)
    cfg = load_voice_config(pack_dir)
    assert isinstance(cfg, VoiceConfig)
    assert cfg.asr is not None
    assert cfg.asr["engine"] == "sherpa-onnx"
    assert cfg.asr["model_id"] == "sense-voice-zh-en-ja-ko-yue"
    assert cfg.tts is not None
    assert cfg.tts["engine"] == "luxtts-onnx"
    # Relative prompt path resolved against pack_dir
    assert cfg.tts["prompt"] == pack_dir / "voice/luxtts/prompt.npz"


def test_load_voice_config_absent_returns_no_voice(tmp_path: Path):
    pack_dir = tmp_path / "pack"
    _write_pack(pack_dir, with_voice=False)
    cfg = load_voice_config(pack_dir)
    assert cfg == no_voice_config()
    assert cfg.asr is None
    assert cfg.tts is None


def test_load_voice_config_missing_asr_section_ok(tmp_path: Path):
    """A pack can have TTS without ASR (or vice versa)."""
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "doll.toml").write_text(
        '[meta]\nid = "test"\nname = "Test"\n[identity]\nself=""\npersonality=""\ntaboos=""\n'
    )
    (pack_dir / "voice").mkdir()
    (pack_dir / "voice" / "engine.toml").write_text(
        """
[tts]
engine = "luxtts-onnx"
prompt = "voice/luxtts/prompt.npz"
"""
    )
    cfg = load_voice_config(pack_dir)
    assert cfg.asr is None
    assert cfg.tts is not None
