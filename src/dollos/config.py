"""Configuration: TOML loading + pydantic validation."""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class LLMConfig(BaseModel):
    backend: Literal["llamacpp"] = "llamacpp"
    base_url: str
    model_alias: str
    timeout_s: float = 60.0


class IPCConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 9876


class LogConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class MemoryConfig(BaseModel):
    db_path: Path

    @field_validator("db_path", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v


class EmbedderConfig(BaseModel):
    backend: Literal["llamacpp"] = "llamacpp"
    base_url: str
    model_id: str
    timeout_s: float = 30.0


class Settings(BaseModel):
    llm: LLMConfig
    ipc: IPCConfig = Field(default_factory=lambda: IPCConfig())
    log: LogConfig = Field(default_factory=lambda: LogConfig())
    memory: MemoryConfig
    embedder: EmbedderConfig


def load_settings(path: Path) -> Settings:
    """Load and validate a TOML config file."""
    with path.open("rb") as f:
        data = tomllib.load(f)
    return Settings.model_validate(data)
