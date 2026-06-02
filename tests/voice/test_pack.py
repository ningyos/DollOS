"""Voice config loading from character packs (per-engine variant schema)."""
from __future__ import annotations

from pathlib import Path

import pytest

from dollos.voice.pack import (
    VoiceConfig,
    load_voice_config,
    no_voice_config,
    resolve_voice_kwargs,
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
[tts.luxtts-onnx]
prompt_path = "voice/luxtts/prompt.npz"
"""
        )


def test_load_voice_config_present(tmp_path: Path):
    pack_dir = tmp_path / "pack"
    _write_pack(pack_dir, with_voice=True)
    cfg = load_voice_config(pack_dir)
    assert isinstance(cfg, VoiceConfig)
    assert cfg.tts is not None
    assert "luxtts-onnx" in cfg.tts
    assert cfg.tts["luxtts-onnx"]["prompt_path"] == pack_dir / "voice/luxtts/prompt.npz"


def test_load_voice_config_absent_returns_no_voice(tmp_path: Path):
    pack_dir = tmp_path / "pack"
    _write_pack(pack_dir, with_voice=False)
    cfg = load_voice_config(pack_dir)
    assert cfg == no_voice_config()
    assert cfg.tts is None


def test_load_voice_config_resolves_ref_audio(tmp_path: Path):
    """ref_audio in [tts.qwen3-tts] is resolved to an absolute pack path."""
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "voice").mkdir()
    (pack_dir / "voice" / "engine.toml").write_text(
        """
[tts.qwen3-tts]
ref_audio = "voice/qwen3/ref.wav"
ref_text = "hi"
instruction = "excited"
"""
    )
    cfg = load_voice_config(pack_dir)
    assert cfg.tts is not None
    assert cfg.tts["qwen3-tts"]["ref_audio"] == pack_dir / "voice/qwen3/ref.wav"


def test_load_voice_config_multiple_variants(tmp_path: Path):
    """A pack can carry identity for multiple TTS engines side-by-side."""
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "voice").mkdir()
    (pack_dir / "voice" / "engine.toml").write_text(
        """
[tts.fish-tts]
voice_profile_path = "voice/fish/v.npy"
transcript = "abc"

[tts.qwen3-tts]
ref_audio = "voice/qwen3/ref.wav"
ref_text = "abc"
instruction = "excited"
"""
    )
    cfg = load_voice_config(pack_dir)
    assert cfg.tts is not None
    assert set(cfg.tts) == {"fish-tts", "qwen3-tts"}
    assert cfg.tts["fish-tts"]["voice_profile_path"] == pack_dir / "voice/fish/v.npy"
    assert cfg.tts["qwen3-tts"]["ref_audio"] == pack_dir / "voice/qwen3/ref.wav"


# ----- resolve_voice_kwargs -----


def test_resolve_voice_kwargs_happy_path(tmp_path: Path):
    cfg = VoiceConfig(
        tts={
            "qwen3-tts": {
                "ref_audio": tmp_path / "ref.wav",
                "ref_text": "hi",
                "instruction": "excited",
            },
        }
    )
    dollos_tts = {
        "engine": "qwen3-tts",
        "model_id": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        "device": "cuda:0",
    }
    name, kwargs = resolve_voice_kwargs(cfg, dollos_tts)
    assert name == "qwen3-tts"
    assert kwargs == {
        "ref_audio": tmp_path / "ref.wav",
        "ref_text": "hi",
        "instruction": "excited",
        "model_id": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        "device": "cuda:0",
    }


def test_resolve_voice_kwargs_missing_variant_raises():
    cfg = VoiceConfig(tts={"fish-tts": {"voice_profile_path": "x"}})
    with pytest.raises(ValueError, match=r"\[tts.qwen3-tts\]"):
        resolve_voice_kwargs(cfg, {"engine": "qwen3-tts"})


def test_resolve_voice_kwargs_no_tts_section_raises():
    with pytest.raises(ValueError, match=r"\[tts.fish-tts\]"):
        resolve_voice_kwargs(VoiceConfig(tts=None), {"engine": "fish-tts"})


def test_resolve_voice_kwargs_missing_engine_key_raises():
    cfg = VoiceConfig(tts={"qwen3-tts": {"ref_text": "x"}})
    with pytest.raises(ValueError, match="engine"):
        resolve_voice_kwargs(cfg, {"model_id": "x"})


def test_load_voice_config_resolves_voice_profile_paths(tmp_path: Path):
    """voice_profile_paths list entries are resolved to absolute pack paths."""
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "voice").mkdir()
    (pack_dir / "voice" / "engine.toml").write_text(
        """
[tts.fish-tts]
voice_profile_paths = [
  "voice/fish/clip_a.npy",
  "voice/fish/clip_b.npy",
]
transcripts = ["text a", "text b"]
"""
    )
    cfg = load_voice_config(pack_dir)
    assert cfg.tts is not None
    paths = cfg.tts["fish-tts"]["voice_profile_paths"]
    assert len(paths) == 2
    assert paths[0] == pack_dir / "voice/fish/clip_a.npy"
    assert paths[1] == pack_dir / "voice/fish/clip_b.npy"
    # transcripts are plain strings, not paths
    assert cfg.tts["fish-tts"]["transcripts"] == ["text a", "text b"]


def test_resolve_voice_kwargs_dollos_wins_on_overlap():
    """DollOS config overrides the pack on overlapping keys.

    Rationale documented in pack.py: pack owns identity; operator owns
    infra/runtime. If both happen to set the same key, the operator's
    override wins so deployment-time tuning sticks.
    """
    cfg = VoiceConfig(tts={"qwen3-tts": {"device": "cpu", "ref_text": "hi"}})
    name, kwargs = resolve_voice_kwargs(
        cfg, {"engine": "qwen3-tts", "device": "cuda:0"}
    )
    assert name == "qwen3-tts"
    assert kwargs["device"] == "cuda:0"
    assert kwargs["ref_text"] == "hi"


# ----- TASK 1: VoiceConfig.engine + parse top-level engine -----


def test_voice_config_engine_defaults_none(tmp_path):
    from dollos.voice.pack import load_voice_config
    vdir = tmp_path / "voice"; vdir.mkdir(parents=True)
    (vdir / "engine.toml").write_text(
        '[tts.luxtts-onnx]\nprompt_path = "voice/luxtts/prompt.npz"\n'
    )
    cfg = load_voice_config(tmp_path)
    assert cfg.engine is None
    assert "luxtts-onnx" in cfg.tts


def test_voice_config_parses_top_level_engine(tmp_path):
    from dollos.voice.pack import load_voice_config
    vdir = tmp_path / "voice"; vdir.mkdir(parents=True)
    (vdir / "engine.toml").write_text(
        'engine = "qwen3-tts"\n'
        '[tts.qwen3-tts]\nref_audio = "voice/qwen3/ref.wav"\nref_text = "hi"\n'
    )
    cfg = load_voice_config(tmp_path)
    assert cfg.engine == "qwen3-tts"
    assert "qwen3-tts" in cfg.tts


# ----- TASK 2: resolve pack-override + per-engine infra -----


def test_resolve_uses_config_engine_when_pack_silent():
    vc = VoiceConfig(tts={"luxtts-onnx": {"prompt_path": "p.npz"}}, engine=None)
    name, kw = resolve_voice_kwargs(vc, {"engine": "luxtts-onnx", "device": "cuda"})
    assert name == "luxtts-onnx"
    assert kw["prompt_path"] == "p.npz"
    assert kw["device"] == "cuda"


def test_resolve_pack_engine_overrides_config():
    vc = VoiceConfig(
        tts={"qwen3-tts": {"ref_audio": "r.wav", "ref_text": "hi"}},
        engine="qwen3-tts",
    )
    cfg_tts = {
        "engine": "luxtts-onnx",
        "luxtts-onnx": {"model_dir": "data/luxtts", "device": "cuda"},
        "qwen3-tts": {"device": "cuda:0"},
    }
    name, kw = resolve_voice_kwargs(vc, cfg_tts)
    assert name == "qwen3-tts"
    assert kw["ref_audio"] == "r.wav"
    assert kw["device"] == "cuda:0"
    assert "model_dir" not in kw


def test_resolve_per_engine_block_for_default_engine():
    vc = VoiceConfig(tts={"luxtts-onnx": {"prompt_path": "p.npz"}}, engine=None)
    cfg_tts = {
        "engine": "luxtts-onnx",
        "luxtts-onnx": {"model_dir": "data/luxtts", "device": "cuda"},
    }
    name, kw = resolve_voice_kwargs(vc, cfg_tts)
    assert name == "luxtts-onnx"
    assert kw["model_dir"] == "data/luxtts"
    assert kw["device"] == "cuda"


def test_resolve_raises_when_pack_lacks_variant():
    import pytest
    vc = VoiceConfig(tts={"luxtts-onnx": {"prompt_path": "p.npz"}}, engine="qwen3-tts")
    with pytest.raises(ValueError, match="qwen3-tts"):
        resolve_voice_kwargs(vc, {"engine": "luxtts-onnx"})
