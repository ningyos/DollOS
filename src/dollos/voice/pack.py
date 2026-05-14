"""Load voice/engine.toml from a character pack."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VoiceConfig:
    """Per-character voice configuration. asr / tts are None when absent."""

    asr: dict | None = None
    tts: dict | None = None


def no_voice_config() -> VoiceConfig:
    return VoiceConfig()


def _resolve_path_fields(section: dict, pack_dir: Path) -> dict:
    """Resolve relative path-like fields against pack_dir.

    Recognized path keys: `model_dir`, `prompt_path`. Absolute paths are kept.
    Keys match the constructor kwargs of the engine classes so the dict
    can be passed through as **kwargs.
    """
    out = dict(section)
    for key in ("model_dir", "prompt_path", "voice_profile_path", "voice_onnx_path", "voice_config_path"):
        if key in out and isinstance(out[key], str):
            p = Path(out[key])
            out[key] = p if p.is_absolute() else (pack_dir / p)
    return out


def load_voice_config(pack_dir: Path) -> VoiceConfig:
    """Read pack_dir/voice/engine.toml. Returns empty config if file absent.

    Relative `model_dir` / `prompt_path` paths are resolved against pack_dir.
    """
    engine_toml = pack_dir / "voice" / "engine.toml"
    if not engine_toml.exists():
        return no_voice_config()
    with engine_toml.open("rb") as f:
        raw = tomllib.load(f)
    asr = _resolve_path_fields(raw["asr"], pack_dir) if "asr" in raw else None
    tts = _resolve_path_fields(raw["tts"], pack_dir) if "tts" in raw else None
    return VoiceConfig(asr=asr, tts=tts)
