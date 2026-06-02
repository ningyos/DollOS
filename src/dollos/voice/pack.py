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
    "voice_clone_prompt_path",
    "eq_curve_path",
)

# Keys whose value is a list[str] of relative paths — each entry is resolved.
_PATH_LIST_KEYS = (
    "voice_profile_paths",
)


@dataclass(frozen=True)
class VoiceConfig:
    """Per-character voice configuration.

    ``tts`` maps engine name → identity kwargs for that engine (e.g.
    ``{"qwen3-tts": {"ref_audio": Path(...), "ref_text": "..."}}``).
    ``None`` when the pack has no ``voice/engine.toml`` or it has no
    ``[tts]`` section.

    ``engine`` is an optional top-level override declared in the pack's
    ``voice/engine.toml`` as a bare ``engine = "..."`` key (sibling of the
    ``[tts.*]`` tables).  When set it overrides the DollOS-global
    ``[voice.tts] engine`` for this character.  ``None`` means the pack
    defers to the global default.
    """

    tts: dict[str, dict] | None = None
    engine: str | None = None


def no_voice_config() -> VoiceConfig:
    return VoiceConfig()


def _resolve_path_fields(section: dict, pack_dir: Path) -> dict:
    """Resolve relative path-like fields against ``pack_dir``.

    Absolute paths are kept as-is.  List-of-path fields (``_PATH_LIST_KEYS``)
    have each element resolved individually.
    """
    out = dict(section)
    for key in _PATH_KEYS:
        if key in out and isinstance(out[key], str):
            p = Path(out[key])
            out[key] = p if p.is_absolute() else (pack_dir / p)
    for key in _PATH_LIST_KEYS:
        if key in out and isinstance(out[key], list):
            resolved = []
            for entry in out[key]:
                if isinstance(entry, str):
                    p = Path(entry)
                    resolved.append(p if p.is_absolute() else (pack_dir / p))
                else:
                    resolved.append(entry)
            out[key] = resolved
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
    # Optional top-level engine override (bare key, NOT inside [tts.*]).
    pack_engine: str | None = raw.get("engine")

    raw_tts = raw.get("tts")
    if not isinstance(raw_tts, dict) or not raw_tts:
        return VoiceConfig(engine=pack_engine) if pack_engine else no_voice_config()
    tts: dict[str, dict] = {}
    for engine_name, variant in raw_tts.items():
        if not isinstance(variant, dict):
            raise ValueError(
                f"voice/engine.toml: [tts.{engine_name}] must be a table"
            )
        tts[engine_name] = _resolve_path_fields(variant, pack_dir)
    return VoiceConfig(tts=tts, engine=pack_engine)


def resolve_voice_kwargs(
    voice_cfg: VoiceConfig, dollos_voice_tts: dict
) -> tuple[str, dict]:
    """Merge pack identity with DollOS infra into a single kwargs dict.

    Engine selection (highest priority first):

    1. ``voice_cfg.engine`` — pack-declared override (``engine = "..."`` top
       key in ``voice/engine.toml``).
    2. ``dollos_voice_tts["engine"]`` — global DollOS ``[voice.tts] engine``.

    Infra merging for the resolved engine:

    - Start with the pack's ``[tts.<engine>]`` identity block.
    - If the resolved engine matches the global default, flat (non-dict)
      keys from ``dollos_voice_tts`` (other than ``"engine"``) are merged in.
    - Then the per-engine infra block ``dollos_voice_tts[engine_name]`` (if
      present) is merged on top — operator wins on overlapping keys.

    Args:
        voice_cfg: pack-loaded :class:`VoiceConfig` (per-engine identity +
            optional pack engine override).
        dollos_voice_tts: ``[voice.tts]`` block from DollOS config; MUST
            contain an ``"engine"`` key.  May also contain nested per-engine
            dicts (``"luxtts-onnx": {...}``, ``"qwen3-tts": {...}``) and flat
            infra keys (``device``, ``model_id``, ...).

    Returns:
        ``(engine_name, merged_kwargs)`` ready for
        ``TTS_REGISTRY[engine_name](**merged_kwargs)``.

    Raises:
        ValueError: if no engine can be determined, or the pack has no
            matching ``[tts.<engine>]`` variant.
    """
    default_engine = dollos_voice_tts.get("engine")
    engine_name = voice_cfg.engine or default_engine
    if engine_name is None:
        raise ValueError(
            "no TTS engine: pack declares none and [voice.tts] has no 'engine'"
        )
    if voice_cfg.tts is None or engine_name not in voice_cfg.tts:
        raise ValueError(f"character pack lacks [tts.{engine_name}] variant")

    per_engine = {k: v for k, v in dollos_voice_tts.items() if isinstance(v, dict)}
    flat_infra = {
        k: v
        for k, v in dollos_voice_tts.items()
        if k != "engine" and not isinstance(v, dict)
    }

    merged = dict(voice_cfg.tts[engine_name])
    if engine_name == default_engine:
        merged.update(flat_infra)
    merged.update(per_engine.get(engine_name, {}))
    return engine_name, merged
