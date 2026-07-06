"""Tests for systemd user-unit generation (P1g Task 1; single-service Task D/5).

Pure string-template + path-resolution tests — no systemd interaction.

Single-service migration (spec §7 / `2026-07-06-bridge-internalization-design.md`):
the daemon now internalizes the Discord bridge as a supervised subprocess
(`ServiceSupervisor`), so `render_bridge_unit` and the bridge-only
`UnitParams` fields (`bridge_config`, `daemon_ws`, `retention_days`) are
gone. `render_daemon_unit` / `resolve_params` for the daemon are unchanged.
"""

import sys
from pathlib import Path

import pytest

from dollos.ctl.units import UnitParams, render_daemon_unit, resolve_params


def _params(**overrides) -> UnitParams:
    defaults = dict(
        python="/venv/bin/python",
        working_dir="/wd",
        daemon_config="/c/d.toml",
        data_root="/wd/data",
    )
    defaults.update(overrides)
    return UnitParams(**defaults)


def test_render_bridge_unit_no_longer_exists():
    """`render_bridge_unit` was deleted wholesale — the bridge unit is gone,
    not just unused. Import-by-name must fail."""
    import dollos.ctl.units as units_mod

    assert not hasattr(units_mod, "render_bridge_unit")


def test_unit_params_has_no_bridge_only_fields():
    """`UnitParams` must not carry the bridge-only fields the removed
    `render_bridge_unit` used to consume."""
    p = _params()
    for bridge_field in ("bridge_config", "daemon_ws", "retention_days"):
        assert not hasattr(p, bridge_field)


def test_daemon_unit_contains_exec_start_and_restart_policy():
    p = _params()
    u = render_daemon_unit(p)
    assert 'ExecStart="/venv/bin/python" -m dollos --config "/c/d.toml"' in u
    assert "Restart=on-failure" in u
    assert "WantedBy=default.target" in u
    assert "WorkingDirectory=/wd" in u


def test_daemon_unit_kill_mode_is_mixed():
    """P1g whole-branch review minor #5: KillMode=mixed so `systemctl
    stop`/`restart` SIGTERMs only the daemon process (not the whole
    cgroup), letting the daemon's own SIGTERM handler drive the
    orchestrated SIGINT-to-bridge graceful close instead of systemd's
    default `control-group` hard-killing the bridge child too."""
    p = _params()
    u = render_daemon_unit(p)
    assert "KillMode=mixed" in u


def test_daemon_unit_restart_sec_is_configurable():
    p = _params(restart_sec=7)
    u = render_daemon_unit(p)
    assert "RestartSec=7" in u


def test_exec_start_quotes_paths_containing_spaces():
    """Minor #2 (P1g whole-branch review): systemd splits ExecStart on
    whitespace, so an unquoted path with a space (e.g. under `My Projects/`)
    would break the command. Double-quoting is systemd's supported escape."""
    p = UnitParams(
        python="/My Venv/bin/python",
        working_dir="/My Projects/DollOS",
        daemon_config="/My Projects/DollOS/config.toml",
        data_root="/My Projects/DollOS/data",
    )
    daemon_u = render_daemon_unit(p)

    assert '"/My Venv/bin/python"' in daemon_u
    assert '"/My Projects/DollOS/config.toml"' in daemon_u


def test_resolve_params_absolutizes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = resolve_params(
        daemon_config=Path("config.toml"),
        data_root=Path("data"),
    )
    assert Path(p.daemon_config).is_absolute()
    assert Path(p.data_root).is_absolute()
    assert Path(p.working_dir).is_absolute()
    assert p.python == sys.executable
    assert p.working_dir == str(tmp_path.resolve())


def test_resolve_params_expands_user_and_accepts_explicit_python_and_working_dir(tmp_path):
    p = resolve_params(
        daemon_config=tmp_path / "d.toml",
        data_root=tmp_path / "data",
        python="/custom/python",
        working_dir=tmp_path,
    )
    assert p.python == "/custom/python"
    assert p.working_dir == str(tmp_path.resolve())


def test_resolve_params_rejects_stray_bridge_config_kwarg():
    """`resolve_params` no longer accepts a `bridge_config` kwarg — the
    bridge config path is now consumed by the daemon's `[bridge].config`,
    not by dollosctl at all."""
    with pytest.raises(TypeError):
        resolve_params(
            daemon_config=Path("/c/d.toml"),
            bridge_config=Path("/c/b.toml"),
            data_root=Path("/wd/data"),
        )
