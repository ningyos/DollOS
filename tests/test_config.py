"""Tests for config loading."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from dollos.config import BridgeConfig, Settings, SystemPulseConfig, load_settings


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
    """[memory] now exists (primary_language) but has extra='forbid', so the
    legacy `db_path` field still produces a validation error."""
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
"""
    )
    with pytest.raises(ValidationError):
        load_settings(config_path)


def test_settings_includes_conversation_history_default(tmp_path: Path) -> None:
    """Conversation history config includes default max_turns."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(_BASE_TOML)
    settings = load_settings(config_path)
    assert settings.conversation_history.max_turns == 6


def test_settings_conversation_history_custom_max_turns(tmp_path: Path) -> None:
    """Conversation history max_turns can be customized."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _BASE_TOML
        + """
[conversation_history]
max_turns = 10
"""
    )
    settings = load_settings(config_path)
    assert settings.conversation_history.max_turns == 10


def test_memory_primary_language_defaults_to_traditional_chinese(tmp_path: Path) -> None:
    """[memory] is optional; primary_language defaults to 繁體中文 when absent."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(_BASE_TOML)
    settings = load_settings(config_path)
    assert settings.memory.primary_language == "繁體中文"


def test_memory_primary_language_override(tmp_path: Path) -> None:
    """primary_language can be configured via the [memory] section."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _BASE_TOML
        + """
[memory]
primary_language = "English"
"""
    )
    settings = load_settings(config_path)
    assert settings.memory.primary_language == "English"


def test_consolidation_config_defaults(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(_BASE_TOML)
    s = load_settings(config_path)
    assert s.consolidation.idle_threshold_s == 300
    assert s.consolidation.min_interval_s == 3600
    assert s.consolidation.enabled is True


def test_energy_config_defaults():
    from dollos.config import EnergyConfig
    c = EnergyConfig()
    assert c.enabled is True and c.cost_per_turn == 0.05
    assert c.restore_per_tick == 0.05 and c.idle_threshold_s == 600 and c.restore_debounce_s == 300


def test_energy_config_on_settings(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(_BASE_TOML)
    s = load_settings(config_path)
    assert s.energy.enabled is True
    assert s.energy.cost_per_turn == 0.05
    assert s.energy.restore_per_tick == 0.05
    assert s.energy.idle_threshold_s == 600
    assert s.energy.restore_debounce_s == 300


def test_self_profile_config_defaults():
    from dollos.config import Settings, LLMConfig, CharacterConfig
    s = Settings(
        llm=LLMConfig(base_url="http://x", model_alias="m"),
        character=CharacterConfig(pack="gura"),
    )
    assert s.self_profile.enabled is True
    assert s.self_profile.max_chars == 1200


def test_diary_config_defaults():
    from dollos.config import Settings, LLMConfig, CharacterConfig
    s = Settings(
        llm=LLMConfig(base_url="http://x", model_alias="m"),
        character=CharacterConfig(pack="gura"),
    )
    assert s.diary.enabled is True
    assert s.diary.hour == 23 and s.diary.minute == 0
    assert s.diary.max_log_chars == 40000


def test_trace_settings_default_enabled():
    from dollos.config import Settings, LLMConfig, CharacterConfig
    s = Settings(
        llm=LLMConfig(base_url="http://x", model_alias="m"),
        character=CharacterConfig(pack="gura"),
    )
    assert s.trace.enabled is True
    assert s.trace.root == "data/traces"


def test_attention_settings_defaults():
    from dollos.config import AttentionSettings
    c = AttentionSettings()
    assert c.name_aliases == []
    assert c.always_wake_channels == []
    assert c.max_session_turns == 6
    assert c.window_base_s == 90.0
    assert c.window_decay == 0.6
    assert c.debounce_engaged_s == 2.0
    assert c.debounce_cold_s == 8.0


def test_attention_settings_on_settings_default(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(_BASE_TOML)
    s = load_settings(config_path)
    assert s.attention.name_aliases == []
    assert s.attention.max_session_turns == 6


def test_attention_settings_override(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _BASE_TOML
        + """
[attention]
name_aliases = ["gura", "古拉"]
always_wake_channels = ["vip"]
max_session_turns = 4
window_base_s = 60.0
"""
    )
    s = load_settings(config_path)
    assert s.attention.name_aliases == ["gura", "古拉"]
    assert s.attention.always_wake_channels == ["vip"]
    assert s.attention.max_session_turns == 4
    assert s.attention.window_base_s == 60.0


def test_attention_settings_rejects_unknown_field(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _BASE_TOML
        + """
[attention]
bogus_field = 1
"""
    )
    with pytest.raises(ValidationError):
        load_settings(config_path)


def _minimal_settings_dict(tmp_path):
    # LLMConfig + CharacterConfig 是 Settings 僅有的 required 欄位;其餘有預設。
    # NOTE: LLMConfig's required fields are `base_url` + `model_alias` (not
    # `model_id` — confirmed against src/dollos/config.py, adjusted from the
    # task brief's draft helper to match the real schema).
    return {
        "llm": {"base_url": "http://localhost:8001", "model_alias": "x"},
        "character": {"pack": str(tmp_path / "pack")},
    }


def test_bridge_defaults_disabled():
    cfg = BridgeConfig()
    assert cfg.enabled is False
    assert cfg.config is None


def test_bridge_enabled_requires_config():
    with pytest.raises(ValidationError, match="config"):
        BridgeConfig(enabled=True)


def test_bridge_config_expands_user():
    cfg = BridgeConfig(enabled=True, config="~/bridge.toml")
    assert cfg.config == Path("~/bridge.toml").expanduser()
    assert cfg.config.is_absolute()


def test_bridge_forbids_extra_keys():
    # 舊的 restart 旋鈕若被誤留在 TOML,必須被拒(防遺留)。
    with pytest.raises(ValidationError):
        BridgeConfig(enabled=True, config="bridge.toml", backoff_max_s=99)


def test_settings_without_bridge_block_defaults_disabled(tmp_path):
    s = Settings.model_validate(_minimal_settings_dict(tmp_path))
    assert s.bridge.enabled is False


def test_settings_with_bridge_block(tmp_path):
    d = _minimal_settings_dict(tmp_path)
    d["bridge"] = {"enabled": True, "config": "bridge.toml"}
    s = Settings.model_validate(d)
    assert s.bridge.enabled is True
    assert s.bridge.config == Path("bridge.toml")


def test_system_pulse_alert_defaults():
    c = SystemPulseConfig()
    assert c.alerts_enabled is True
    assert c.alert_throttle_s == 900.0
    assert c.window_stuck_s == 5400.0


def test_system_pulse_alert_override():
    c = SystemPulseConfig(alerts_enabled=False, alert_throttle_s=60.0, window_stuck_s=120.0)
    assert c.alerts_enabled is False
    assert c.alert_throttle_s == 60.0
    assert c.window_stuck_s == 120.0
