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
        if isinstance(v, str):
            return Path(v).expanduser()
        return v


class MemsearchConfig(BaseModel):
    """memsearch knobs (paths derived from data.root in kernel.build_memsearch)."""
    model_config = ConfigDict(extra="forbid")

    top_k: int = 10


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
        if isinstance(v, str):
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


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm: LLMConfig
    ipc: IPCConfig = Field(default_factory=lambda: IPCConfig())
    log: LogConfig = Field(default_factory=lambda: LogConfig())
    data: DataConfig = Field(default_factory=lambda: DataConfig())
    memsearch: MemsearchConfig = Field(default_factory=lambda: MemsearchConfig())
    conversation_history: ConversationHistoryConfig = Field(
        default_factory=lambda: ConversationHistoryConfig()
    )
    character: CharacterConfig
    voice: VoiceSettings = Field(default_factory=lambda: VoiceSettings())


def load_settings(path: Path) -> Settings:
    """Load and validate a TOML config file."""
    with path.open("rb") as f:
        data = tomllib.load(f)
    return Settings.model_validate(data)
