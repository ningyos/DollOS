"""P1 Task 4: mcp.toml [server].bind_host fail-closed loopback guard (spec §E).

_load_mcp_config MUST raise on any non-loopback bind_host — there is no
network-layer auth to fall back to, so the invariant is enforced in code,
not just documentation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dollos.mcp_server.__main__ import _load_mcp_config


def _write_toml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "mcp.toml"
    p.write_text(body)
    return p


def test_non_loopback_bind_host_raises(tmp_path: Path):
    cfg = _write_toml(tmp_path, '[server]\nbind_host = "0.0.0.0"\nbind_port = 9877\n')
    with pytest.raises(ValueError, match="not loopback"):
        _load_mcp_config(cfg)


def test_loopback_bind_host_accepted(tmp_path: Path):
    cfg = _write_toml(tmp_path, '[server]\nbind_host = "127.0.0.1"\nbind_port = 9877\n')
    assert _load_mcp_config(cfg) == ("127.0.0.1", 9877)


def test_missing_server_table_defaults_to_loopback(tmp_path: Path):
    cfg = _write_toml(tmp_path, "")
    assert _load_mcp_config(cfg) == ("127.0.0.1", 9877)
