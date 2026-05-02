"""Tests for config loading."""

from pathlib import Path

import pytest

from pydantic import ValidationError

from dollos.config import Settings, load_settings


def test_load_settings_from_toml(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
provider = "llamacpp"
template = "qwen3-thinking"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[ipc]
host = "127.0.0.1"
port = 9876

[log]
level = "INFO"

[memory]
db_path = "/tmp/dollos/memory.db"

[embedder]
backend = "llamacpp"
base_url = "http://127.0.0.1:8002"
model_id = "bge-base-en-v1.5"
"""
    )

    settings = load_settings(config_path)

    assert isinstance(settings, Settings)
    assert settings.llm.provider == "llamacpp"
    assert settings.llm.template == "qwen3-thinking"
    assert settings.llm.base_url == "http://127.0.0.1:8001"
    assert settings.llm.model_alias == "test-model"
    assert settings.ipc.host == "127.0.0.1"
    assert settings.ipc.port == 9876
    assert settings.log.level == "INFO"


def test_load_settings_missing_required_field(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
provider = "llamacpp"
template = "qwen3-thinking"
# missing base_url and model_alias
"""
    )

    with pytest.raises(ValueError):
        load_settings(config_path)


def test_load_settings_default_log_level(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
provider = "llamacpp"
template = "qwen3-thinking"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[ipc]
host = "127.0.0.1"
port = 9876

[memory]
db_path = "/tmp/dollos/memory.db"

[embedder]
backend = "llamacpp"
base_url = "http://127.0.0.1:8002"
model_id = "bge-base-en-v1.5"
"""
    )

    settings = load_settings(config_path)

    assert settings.log.level == "INFO"


def test_load_settings_includes_memory_and_embedder(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
provider = "llamacpp"
template = "qwen3-thinking"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[ipc]
host = "127.0.0.1"
port = 9876

[log]
level = "INFO"

[memory]
db_path = "/tmp/dollos/memory.db"

[embedder]
backend = "llamacpp"
base_url = "http://127.0.0.1:8002"
model_id = "bge-base-en-v1.5"
timeout_s = 30.0
"""
    )

    settings = load_settings(config_path)

    assert str(settings.memory.db_path) == "/tmp/dollos/memory.db"
    assert settings.embedder.backend == "llamacpp"
    assert settings.embedder.base_url == "http://127.0.0.1:8002"
    assert settings.embedder.model_id == "bge-base-en-v1.5"
    assert settings.embedder.timeout_s == 30.0


def test_settings_db_path_expands_user(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
provider = "llamacpp"
template = "qwen3-thinking"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[ipc]
host = "127.0.0.1"
port = 9876

[memory]
db_path = "~/dollos-memory.db"

[embedder]
backend = "llamacpp"
base_url = "http://127.0.0.1:8002"
model_id = "test-emb"
"""
    )
    settings = load_settings(config_path)
    assert "~" not in str(settings.memory.db_path)
    assert str(settings.memory.db_path).endswith("dollos-memory.db")


def test_load_settings_old_backend_field_raises(tmp_path: Path):
    """Pre-Plan-3 configs used `backend = "llamacpp"`. After the rename
    that field is unknown and pydantic should reject the missing
    required `provider`."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
backend = "llamacpp"
template = "qwen3-thinking"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[ipc]
host = "127.0.0.1"
port = 9876

[memory]
db_path = "/tmp/dollos/memory.db"

[embedder]
backend = "llamacpp"
base_url = "http://127.0.0.1:8002"
model_id = "test-emb"
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

[ipc]
host = "127.0.0.1"
port = 9876

[memory]
db_path = "/tmp/dollos/memory.db"

[embedder]
backend = "llamacpp"
base_url = "http://127.0.0.1:8002"
model_id = "test-emb"
"""
    )

    with pytest.raises(ValidationError):
        load_settings(config_path)
