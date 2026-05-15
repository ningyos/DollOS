"""Load voice/engine.toml from a character pack.

Schema (post-refactor): character packs carry ONLY per-character identity
data, keyed by engine variant.  Infrastructure (engine selection, model_id,
device, sampling defaults) lives in DollOS-level config.

Example pack ``voice/engine.toml``::

    [tts.qwen3-tts]
    ref_audio = "voice/qwen3/ref.wav"
    ref_text = "..."
    instruction = "excited, energetic, animated"

    [tts.fish-tts]
    voice_profile_path = "voice/fish/voice.npy"
    transcript = "..."

ASR is character-agnostic and no longer lives in the pack.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


_PATH_KEYS = (
    "model_dir",
    "prompt_path",
    "voice_profile_path",
    "voice_onnx_path",
    "voice_config_path",
    "ref_audio",
)


@dataclass(frozen=True)
class VoiceConfig:
    """Per-character voice configuration.

    ``tts`` maps engine name → identity kwargs for that engine (e.g.
    ``{"qwen3-tts": {"ref_audio": Path(...), "ref_text": "..."}}``).
    ``None`` when the pack has no ``voice/engine.toml`` or it has no
    ``[tts]`` section.
    """

    tts: dict[str, dict] | None = None


def no_voice_config() -> VoiceConfig:
    return VoiceConfig()


def _resolve_path_fields(section: dict, pack_dir: Path) -> dict:
    """Resolve relative path-like fields against ``pack_dir``.

    Absolute paths are kept as-is.
    """
    out = dict(section)
    for key in _PATH_KEYS:
        if key in out and isinstance(out[key], str):
            p = Path(out[key])
            out[key] = p if p.is_absolute() else (pack_dir / p)
    return out


def load_voice_config(pack_dir: Path) -> VoiceConfig:
    """Read ``pack_dir/voice/engine.toml``.

    Returns empty config if file is absent or has no ``[tts]`` section.
    Each ``[tts.<engine_name>]`` sub-table becomes one entry in
    ``VoiceConfig.tts`` with relative paths resolved against ``pack_dir``.
    """
    pack_dir = Path(pack_dir)
    engine_toml = pack_dir / "voice" / "engine.toml"
    if not engine_toml.exists():
        return no_voice_config()
    with engine_toml.open("rb") as f:
        raw = tomllib.load(f)
    raw_tts = raw.get("tts")
    if not isinstance(raw_tts, dict) or not raw_tts:
        return no_voice_config()
    tts: dict[str, dict] = {}
    for engine_name, variant in raw_tts.items():
        if not isinstance(variant, dict):
            raise ValueError(
                f"voice/engine.toml: [tts.{engine_name}] must be a table"
            )
        tts[engine_name] = _resolve_path_fields(variant, pack_dir)
    return VoiceConfig(tts=tts)


def resolve_voice_kwargs(
    voice_cfg: VoiceConfig, dollos_voice_tts: dict
) -> tuple[str, dict]:
    """Merge pack identity with DollOS infra into a single kwargs dict.

    Args:
        voice_cfg: pack-loaded :class:`VoiceConfig` (per-engine identity).
        dollos_voice_tts: ``[voice.tts]`` block from DollOS config; MUST
            contain an ``"engine"`` key plus any infra/sampling kwargs.

    Returns:
        ``(engine_name, merged_kwargs)`` ready for
        ``TTS_REGISTRY[engine_name](**merged_kwargs)``.

    Raises:
        ValueError: if ``dollos_voice_tts`` lacks ``engine``, or the pack
            has no matching ``[tts.<engine>]`` variant.

    Merge precedence: **DollOS config wins** on overlapping keys.  The pack
    owns character identity; the operator owns infra/runtime knobs
    (model_id, device, sampling).  If the operator sets something in
    DollOS config, that override takes effect even when an identity-style
    key happens to overlap.
    """
    if "engine" not in dollos_voice_tts:
        raise ValueError("DollOS [voice.tts] is missing required 'engine' key")
    engine_name = dollos_voice_tts["engine"]
    if voice_cfg.tts is None or engine_name not in voice_cfg.tts:
        raise ValueError(
            f"character pack lacks [tts.{engine_name}] variant"
        )
    merged = dict(voice_cfg.tts[engine_name])
    for k, v in dollos_voice_tts.items():
        if k == "engine":
            continue
        merged[k] = v
    return engine_name, merged
