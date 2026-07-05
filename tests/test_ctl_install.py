"""Tests for `dollosctl install`/`uninstall` (P1g Task 3).

Writes real unit files to a tmp_path directory (no real systemd
touched — `systemctl.daemon_reload`/`stop` are mocked). Focuses on:
idempotency (re-running install overwrites, doesn't stack; re-running
uninstall on an already-torn-down state doesn't crash), and that no
secret value from the bridge config path leaks into unit content
(units only ever reference the config *path*, never its contents).
"""

import sys
from pathlib import Path
from unittest.mock import call, patch

import pytest

from dollos.ctl.cli import BRIDGE_UNIT, DAEMON_UNIT, install, uninstall
from dollos.ctl.systemctl import SystemctlError


def _install_kwargs(tmp_path: Path, **overrides) -> dict:
    defaults = dict(
        unit_dir=tmp_path / "units",
        daemon_config=tmp_path / "daemon.toml",
        bridge_config=tmp_path / "bridge.toml",
        data_root=tmp_path / "data",
    )
    defaults.update(overrides)
    return defaults


def test_install_writes_both_unit_files_with_absolute_paths_and_reloads(tmp_path):
    kwargs = _install_kwargs(tmp_path)
    with patch("dollos.ctl.cli.systemctl.daemon_reload") as mock_reload:
        install(**kwargs)

    unit_dir = kwargs["unit_dir"]
    daemon_unit = unit_dir / DAEMON_UNIT
    bridge_unit = unit_dir / BRIDGE_UNIT
    assert daemon_unit.exists()
    assert bridge_unit.exists()

    daemon_content = daemon_unit.read_text()
    bridge_content = bridge_unit.read_text()
    assert str(kwargs["daemon_config"].resolve()) in daemon_content
    assert str(kwargs["bridge_config"].resolve()) in bridge_content
    assert str(kwargs["data_root"].resolve()) in bridge_content

    mock_reload.assert_called_once_with()


def test_install_is_idempotent_overwrites_not_appends(tmp_path):
    kwargs = _install_kwargs(tmp_path)
    with patch("dollos.ctl.cli.systemctl.daemon_reload"):
        install(**kwargs)
        first_daemon_content = (kwargs["unit_dir"] / DAEMON_UNIT).read_text()
        install(**kwargs)
        second_daemon_content = (kwargs["unit_dir"] / DAEMON_UNIT).read_text()

    files = sorted(p.name for p in kwargs["unit_dir"].iterdir())
    assert files == sorted([DAEMON_UNIT, BRIDGE_UNIT])
    assert first_daemon_content == second_daemon_content


def test_install_creates_unit_dir_if_missing(tmp_path):
    kwargs = _install_kwargs(tmp_path, unit_dir=tmp_path / "nested" / "units")
    with patch("dollos.ctl.cli.systemctl.daemon_reload"):
        install(**kwargs)
    assert (kwargs["unit_dir"] / DAEMON_UNIT).exists()


def test_uninstall_stops_both_units_deletes_files_and_reloads(tmp_path):
    kwargs = _install_kwargs(tmp_path)
    unit_dir = kwargs["unit_dir"]
    with patch("dollos.ctl.cli.systemctl.daemon_reload"):
        install(**kwargs)

    with (
        patch("dollos.ctl.cli.systemctl.stop") as mock_stop,
        patch("dollos.ctl.cli.systemctl.daemon_reload") as mock_reload,
    ):
        uninstall(unit_dir=unit_dir)

    mock_stop.assert_has_calls([call(BRIDGE_UNIT), call(DAEMON_UNIT)])
    assert not (unit_dir / DAEMON_UNIT).exists()
    assert not (unit_dir / BRIDGE_UNIT).exists()
    mock_reload.assert_called_once_with()


def test_uninstall_missing_files_does_not_crash(tmp_path):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    with (
        patch("dollos.ctl.cli.systemctl.stop"),
        patch("dollos.ctl.cli.systemctl.daemon_reload") as mock_reload,
    ):
        uninstall(unit_dir=unit_dir)  # no files present at all
    mock_reload.assert_called_once_with()


def test_uninstall_tolerates_systemctl_error_from_stop_on_not_loaded_unit(tmp_path):
    kwargs = _install_kwargs(tmp_path)
    unit_dir = kwargs["unit_dir"]
    with patch("dollos.ctl.cli.systemctl.daemon_reload"):
        install(**kwargs)

    with (
        patch(
            "dollos.ctl.cli.systemctl.stop",
            side_effect=SystemctlError(["systemctl", "--user", "stop", "x"], 5, "Unit not loaded."),
        ) as mock_stop,
        patch("dollos.ctl.cli.systemctl.daemon_reload") as mock_reload,
    ):
        uninstall(unit_dir=unit_dir)  # must not raise

    assert mock_stop.call_count == 2
    assert not (unit_dir / DAEMON_UNIT).exists()
    assert not (unit_dir / BRIDGE_UNIT).exists()
    mock_reload.assert_called_once_with()


def test_install_prints_resolved_python_working_dir_and_data_root(tmp_path, capsys):
    """The data-dir-cwd footgun (P1g whole-branch review, Important #1): the
    daemon resolves its data root relative to WorkingDirectory, which is
    itself captured from cwd at install time. `install` must echo the
    resolved absolutes so the operator can catch a wrong-cwd install before
    it silently starts a fresh empty data store."""
    kwargs = _install_kwargs(tmp_path)
    with patch("dollos.ctl.cli.systemctl.daemon_reload"):
        install(**kwargs)

    captured = capsys.readouterr()
    assert sys.executable in captured.out
    assert str(Path.cwd().resolve()) in captured.out  # working_dir defaults to cwd
    assert str(kwargs["data_root"].resolve()) in captured.out


def test_no_secret_token_leaks_into_unit_content(tmp_path):
    """Units reference the bridge config *path* only; a token value
    living inside that config file must never appear in the unit."""
    secret_token = "sk-super-secret-discord-token-abc123"
    bridge_config = tmp_path / "bridge.toml"
    bridge_config.write_text(f'token = "{secret_token}"\nowner_discord_id = "12345"\n')

    kwargs = _install_kwargs(tmp_path, bridge_config=bridge_config)
    with patch("dollos.ctl.cli.systemctl.daemon_reload"):
        install(**kwargs)

    unit_dir = kwargs["unit_dir"]
    daemon_content = (unit_dir / DAEMON_UNIT).read_text()
    bridge_content = (unit_dir / BRIDGE_UNIT).read_text()

    assert secret_token not in daemon_content
    assert secret_token not in bridge_content
    # sanity: the path itself IS present (that's the whole point)
    assert str(bridge_config.resolve()) in bridge_content
