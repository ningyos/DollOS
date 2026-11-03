"""Every behavior number lives here, loaded from TOML. Unknown keys are errors."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    model: str
    reasoning_effort: str = "high"
    timeout_s: float = 300.0
    max_tokens: int = 4096


@dataclass(frozen=True)
class DecisionConfig:
    base_url: str = "http://127.0.0.1:1976"
    model: str = "system"
    timeout_s: float = 30.0


@dataclass(frozen=True)
class CoreConfig:
    data_dir: str = "data"
    pack: str = "character_packs/doll2/doll.toml"
    max_internal_passes: int = 4
    max_open_lines: int = 5
    line_daily_steps: int = 20
    idle_first_s: float = 300.0
    idle_every_s: float = 600.0
    shell_timeout_s: float = 600.0
    job_result_inline_chars: int = 4000
    timeline_window_messages: int = 200
    approve_external: bool = True
    approval_timeout_s: float = 1800.0


@dataclass(frozen=True)
class Config:
    llm: LlmConfig
    decision: DecisionConfig
    core: CoreConfig


def _build(cls, raw: dict, section: str):
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"[{section}] unknown keys: {sorted(unknown)}")
    return cls(**raw)


def load(path: Path) -> Config:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    unknown = set(raw) - {"llm", "decision", "core"}
    if unknown:
        raise ValueError(f"unknown sections: {sorted(unknown)}")
    if "llm" not in raw:
        raise ValueError("[llm] is required")
    return Config(
        llm=_build(LlmConfig, raw["llm"], "llm"),
        decision=_build(DecisionConfig, raw.get("decision", {}), "decision"),
        core=_build(CoreConfig, raw.get("core", {}), "core"),
    )
