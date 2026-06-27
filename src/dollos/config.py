"""Configuration: TOML loading + pydantic validation."""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["llamacpp"] = "llamacpp"
    template: Literal["qwen3-thinking"] = "qwen3-thinking"
    base_url: str
    model_alias: str
    timeout_s: float = 60.0
    # concurrent LLM generations; set to your llama-server --parallel (cloud APIs can raise)
    max_concurrency: int = 2


class IPCConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 9876


class LogConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class DataConfig(BaseModel):
    """Root for all DollOS-generated data. data/ 不存在 = fresh launch."""
    model_config = ConfigDict(extra="forbid")

    root: Path = Path("data")

    @field_validator("root", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, (str, Path)):
            return Path(v).expanduser()
        return v


class MemsearchConfig(BaseModel):
    """memsearch knobs (paths derived from data.root in kernel.build_memsearch)."""
    model_config = ConfigDict(extra="forbid")

    top_k: int = 10


class MemoryConfig(BaseModel):
    """Memory-WRITE policy knobs (how Doll records memory, not where it's stored).

    ``primary_language`` is the language Doll writes memory in (NoteMemory /
    diary), regardless of the source language — mixing original-language proper
    nouns / technical terms where natural is fine. It is injected as a per-turn
    behavioral guideline (prompt engineering), NOT enforced by a subsystem.
    """
    model_config = ConfigDict(extra="forbid")

    primary_language: str = "繁體中文"


class ConversationHistoryConfig(BaseModel):
    """Conversation history sliding window — recent turn transcripts prepended
    to every LLM call so Doll sees her own prior reasoning across turns."""
    model_config = ConfigDict(extra="forbid")

    max_turns: int = Field(
        6,
        ge=1,
        le=50,
        description="conversation window size in turns; default 6 (industry standard tail buffer)",
    )


class CharacterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack: Path

    @field_validator("pack", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, (str, Path)):
            return Path(v).expanduser()
        return v


class VoiceASRSettings(BaseModel):
    """DollOS-level ASR config (character-agnostic).

    All extra keys are passed through as kwargs to the ASR engine
    constructor, so engine-specific knobs (model_id, device, etc.) live
    here directly.
    """

    model_config = ConfigDict(extra="allow")

    engine: str


class VoiceTTSSettings(BaseModel):
    """DollOS-level TTS config (infra + engine selection).

    Per-character identity (ref_audio, transcript, instruction, voice
    profile path, ...) lives in the character pack's
    ``voice/engine.toml``.  Everything else (engine selection, model_id,
    device, sampling defaults) lives here and is merged with the pack at
    session-build time.
    """

    model_config = ConfigDict(extra="allow")

    engine: str


class VoiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asr: VoiceASRSettings | None = None
    tts: VoiceTTSSettings | None = None


class SystemPulseConfig(BaseModel):
    """Proprioception poller — surfaces host vitals as a [Self pulse] block."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    poll_interval_s: float = 60.0
    include_active_window: bool = True   # privacy opt-out


class CognitionConfig(BaseModel):
    """Mind-state vitals — surfaces LLM consumption as a [Cognition] block.

    Token quota is purely a "stamina" axis for Doll's self-awareness; it does
    NOT block calls when exceeded. Set ``daily_token_quota = None`` to omit
    the quota line entirely.
    """
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    daily_token_quota: int | None = 500_000
    session_token_quota: int | None = None       # reserved; not yet surfaced
    telemetry_dir_subpath: str = "telemetry"     # relative to data.root
    max_context_tokens: int = 131_072            # Qwen3.6 default


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm: LLMConfig
    ipc: IPCConfig = Field(default_factory=lambda: IPCConfig())
    log: LogConfig = Field(default_factory=lambda: LogConfig())
    data: DataConfig = Field(default_factory=lambda: DataConfig())
    memsearch: MemsearchConfig = Field(default_factory=lambda: MemsearchConfig())
    memory: MemoryConfig = Field(default_factory=lambda: MemoryConfig())
    conversation_history: ConversationHistoryConfig = Field(
        default_factory=lambda: ConversationHistoryConfig()
    )
    character: CharacterConfig
    voice: VoiceSettings = Field(default_factory=lambda: VoiceSettings())
    system_pulse: SystemPulseConfig = Field(default_factory=lambda: SystemPulseConfig())
    cognition: CognitionConfig = Field(default_factory=lambda: CognitionConfig())


def load_settings(path: Path) -> Settings:
    """Load and validate a TOML config file."""
    with path.open("rb") as f:
        data = tomllib.load(f)
    return Settings.model_validate(data)
