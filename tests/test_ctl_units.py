"""Tests for systemd user-unit generation (P1g Task 1).

Pure string-template + path-resolution tests — no systemd interaction.
"""

import sys
from pathlib import Path

from dollos.ctl.units import UnitParams, render_bridge_unit, render_daemon_unit, resolve_params


def _params(**overrides) -> UnitParams:
    defaults = dict(
        python="/venv/bin/python",
        working_dir="/wd",
        daemon_config="/c/d.toml",
        bridge_config="/c/b.toml",
        data_root="/wd/data",
    )
    defaults.update(overrides)
    return UnitParams(**defaults)


def test_daemon_unit_contains_exec_start_and_restart_policy():
    p = _params()
    u = render_daemon_unit(p)
    assert "ExecStart=/venv/bin/python -m dollos --config /c/d.toml" in u
    assert "Restart=on-failure" in u
    assert "WantedBy=default.target" in u
    assert "WorkingDirectory=/wd" in u


def test_daemon_unit_restart_sec_is_configurable():
    p = _params(restart_sec=7)
    u = render_daemon_unit(p)
    assert "RestartSec=7" in u


def test_bridge_unit_soft_deps_daemon_not_hard():
    p = UnitParams(
        python="/venv/bin/python",
        working_dir="/wd",
        daemon_config="/c/d.toml",
        bridge_config="/c/b.toml",
        data_root="/wd/data",
    )
    u = render_bridge_unit(p)
    assert "Wants=dollos-daemon.service" in u and "After=dollos-daemon.service" in u
    assert "Requires=" not in u  # hard-dep would drag bridge down on daemon restart
    assert "--daemon ws://127.0.0.1:9876" in u and "--config /c/b.toml" in u


def test_bridge_unit_contains_all_cli_args():
    p = _params(data_root="/wd/data", retention_days=45)
    u = render_bridge_unit(p)
    assert "--daemon ws://127.0.0.1:9876" in u
    assert "--config /c/b.toml" in u
    assert "--data-root /wd/data" in u
    assert "--retention-days 45" in u
    assert "Restart=on-failure" in u


def test_resolve_params_absolutizes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = resolve_params(
        daemon_config=Path("config.toml"),
        bridge_config=Path("b.toml"),
        data_root=Path("data"),
    )
    assert Path(p.daemon_config).is_absolute()
    assert Path(p.bridge_config).is_absolute()
    assert Path(p.data_root).is_absolute()
    assert Path(p.working_dir).is_absolute()
    assert p.python == sys.executable
    assert p.working_dir == str(tmp_path.resolve())


def test_resolve_params_expands_user_and_accepts_explicit_python_and_working_dir(tmp_path):
    p = resolve_params(
        daemon_config=tmp_path / "d.toml",
        bridge_config=tmp_path / "b.toml",
        data_root=tmp_path / "data",
        python="/custom/python",
        working_dir=tmp_path,
    )
    assert p.python == "/custom/python"
    assert p.working_dir == str(tmp_path.resolve())
