"""Tests for the [mcp] config schema (P1 Task 1) — mirrors BridgeConfig."""
import pytest
from pydantic import ValidationError

from dollos.config import McpConfig, Settings


def test_mcp_defaults_off():
    cfg = McpConfig()
    assert cfg.enabled is False
    assert cfg.config is None


def test_mcp_enabled_without_config_raises():
    with pytest.raises(ValidationError):
        McpConfig(enabled=True)


def test_mcp_enabled_with_config_ok():
    cfg = McpConfig(enabled=True, config="mcp.toml")
    assert cfg.enabled is True
    assert cfg.config is not None
    assert cfg.config.name == "mcp.toml"


def test_mcp_expands_user_in_config_path():
    cfg = McpConfig(enabled=True, config="~/mcp.toml")
    assert "~" not in str(cfg.config)


def test_mcp_extra_key_forbidden():
    with pytest.raises(ValidationError):
        McpConfig(enabled=False, bogus=1)


def test_settings_absent_mcp_table_defaults_off():
    # Settings is extra="forbid" but McpConfig has a default_factory, so a
    # config with no [mcp] table is valid and yields enabled=false.
    assert Settings.model_fields["mcp"].default_factory().enabled is False


def test_mcp_query_token_defaults_none():
    assert McpConfig().query_token is None


def test_mcp_query_token_accepted():
    assert McpConfig(enabled=True, config="mcp.toml", query_token="s3cr3t").query_token == "s3cr3t"
