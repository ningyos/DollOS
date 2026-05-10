"""Tests for config loading."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from dollos.config import Settings, load_settings


_BASE_TOML = """
[llm]
provider = "llamacpp"
template = "qwen3-thinking"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[ipc]
host = "127.0.0.1"
port = 9876

[character]
pack = "character_packs/gura"

[inner_voice]
base_url = "http://127.0.0.1:8003"
"""


def test_load_settings_minimal_uses_defaults_for_data_and_memsearch(tmp_path: Path):
    """[data] and [memsearch] are both optional with sensible defaults."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(_BASE_TOML)

    settings = load_settings(config_path)

    assert isinstance(settings, Settings)
    assert settings.data.root == Path("data")
    assert settings.memsearch.top_k == 10


def test_load_settings_with_data_root_override(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _BASE_TOML
        + """
[data]
root = "/var/lib/dollos"
"""
    )

    settings = load_settings(config_path)
    assert settings.data.root == Path("/var/lib/dollos")


def test_data_root_expands_user(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _BASE_TOML
        + """
[data]
root = "~/my-dollos-data"
"""
    )
    settings = load_settings(config_path)
    assert "~" not in str(settings.data.root)
    assert str(settings.data.root).endswith("my-dollos-data")


def test_load_settings_with_memsearch_top_k_override(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _BASE_TOML
        + """
[memsearch]
top_k = 5
"""
    )
    settings = load_settings(config_path)
    assert settings.memsearch.top_k == 5


def test_load_settings_rejects_legacy_memory_section(tmp_path: Path):
    """Old [memory] section should produce a validation error (extra fields)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _BASE_TOML
        + """
[memory]
db_path = "/tmp/old.db"
"""
    )
    with pytest.raises(ValidationError):
        load_settings(config_path)


def test_load_settings_rejects_legacy_embedder_section(tmp_path: Path):
    """Old [embedder] section should produce a validation error (extra fields)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _BASE_TOML
        + """
[embedder]
backend = "llamacpp"
base_url = "http://127.0.0.1:8002"
model_id = "bge-base-en-v1.5"
"""
    )
    with pytest.raises(ValidationError):
        load_settings(config_path)


def test_load_settings_rejects_unknown_memsearch_field(tmp_path: Path):
    """Memsearch config has extra='forbid'; unknown fields rejected."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _BASE_TOML
        + """
[memsearch]
top_k = 10
embedding_provider = "openai"  # not exposed in v1
"""
    )
    with pytest.raises(ValidationError):
        load_settings(config_path)


def test_character_config_pack_field(tmp_path: Path):
    """[character] uses `pack` (directory path) as of doll-pack plan."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(_BASE_TOML)
    settings = load_settings(config_path)
    assert settings.character.pack == Path("character_packs/gura")


def test_character_config_rejects_legacy_profile_path(tmp_path: Path):
    """Old `profile_path` field must be rejected (extra='forbid')."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
provider = "llamacpp"
template = "qwen3-thinking"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[character]
profile_path = "experiments/test_character.jinja"

[inner_voice]
base_url = "http://127.0.0.1:8003"
"""
    )
    with pytest.raises(ValidationError):
        load_settings(config_path)


def test_load_settings_unknown_provider_raises(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
provider = "vllm"
template = "qwen3-thinking"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[character]
pack = "character_packs/gura"

[inner_voice]
base_url = "http://127.0.0.1:8003"
"""
    )
    with pytest.raises(ValidationError):
        load_settings(config_path)
