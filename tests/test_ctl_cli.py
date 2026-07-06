"""Tests for `dollosctl`'s argparse dispatch (P1g Task 4; single-service Task D/5).

`main(argv) -> int` is the console-script entry point. These tests mock
the `systemctl` module functions plus `install`/`uninstall` themselves
(imported into `dollos.ctl.cli`'s namespace) and assert dispatch: which
function got called, with which unit(s), and in which order. No real
subprocess or systemd is touched anywhere in this file.

Single-service migration (spec §7): the daemon now internalizes the
Discord bridge as a supervised subprocess, so `dollos-bridge.service` is
no longer written by `install`. `BRIDGE_UNIT` is kept as a constant only
because `install`/`uninstall` must actively clean up a *legacy*
(pre-migration) bridge unit that might still be enabled — see
`test_ctl_install.py` for that cleanup's behavioral tests. This file only
covers argparse dispatch: single-unit start/stop/restart/status/logs.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from dollos.ctl.cli import DAEMON_UNIT, main
from dollos.ctl.systemctl import SystemctlError


def test_start_starts_daemon():
    with patch("dollos.ctl.cli.systemctl.start") as mock_start:
        rc = main(["start"])
    assert rc == 0
    mock_start.assert_called_once_with(DAEMON_UNIT)


def test_stop_stops_daemon():
    with patch("dollos.ctl.cli.systemctl.stop") as mock_stop:
        rc = main(["stop"])
    assert rc == 0
    mock_stop.assert_called_once_with(DAEMON_UNIT)


def test_restart_restarts_daemon():
    with patch("dollos.ctl.cli.systemctl.restart") as mock_restart:
        rc = main(["restart"])
    assert rc == 0
    mock_restart.assert_called_once_with(DAEMON_UNIT)


def test_status_queries_daemon_only():
    with patch("dollos.ctl.cli.systemctl.status", return_value="") as mock_status:
        rc = main(["status"])
    assert rc == 0
    mock_status.assert_called_once_with(DAEMON_UNIT)


def test_logs_daemon_follow():
    with patch("dollos.ctl.cli.systemctl.journal", return_value="") as mock_journal:
        rc = main(["logs", "daemon", "-f"])
    assert rc == 0
    mock_journal.assert_called_once_with(DAEMON_UNIT, follow=True, lines=200)


def test_logs_daemon_lines():
    with patch("dollos.ctl.cli.systemctl.journal", return_value="") as mock_journal:
        rc = main(["logs", "daemon", "-n", "50"])
    assert rc == 0
    mock_journal.assert_called_once_with(DAEMON_UNIT, follow=False, lines=50)


def test_logs_bridge_choice_rejected():
    """`logs bridge` is gone — bridge output now lives in the daemon's
    own journal (it's a supervised subprocess, not a separate unit)."""
    with pytest.raises(SystemExit):
        main(["logs", "bridge"])


def test_install_no_bridge_config_arg():
    """`--bridge-config` is gone from the `install` parser — the bridge
    config path is now consumed by the daemon's `[bridge].config`."""
    from dollos.ctl import cli

    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["install", "--daemon-config", "c.toml", "--bridge-config", "b.toml"]
        )


def test_install_called_with_parsed_args(tmp_path):
    with patch("dollos.ctl.cli.install") as mock_install:
        rc = main(
            [
                "install",
                "--daemon-config",
                "c.toml",
                "--data-root",
                "d",
            ]
        )
    assert rc == 0
    mock_install.assert_called_once()
    _, kwargs = mock_install.call_args
    assert kwargs["daemon_config"] == Path("c.toml")
    assert "bridge_config" not in kwargs
    assert kwargs["data_root"] == Path("d")
    assert kwargs["unit_dir"] is not None  # defaults to _user_unit_dir()


def test_install_accepts_explicit_unit_dir(tmp_path):
    with patch("dollos.ctl.cli.install") as mock_install:
        rc = main(
            [
                "install",
                "--daemon-config",
                "c.toml",
                "--data-root",
                "d",
                "--unit-dir",
                str(tmp_path),
            ]
        )
    assert rc == 0
    _, kwargs = mock_install.call_args
    assert kwargs["unit_dir"] == tmp_path


def test_uninstall_called(tmp_path):
    with patch("dollos.ctl.cli.uninstall") as mock_uninstall:
        rc = main(["uninstall", "--unit-dir", str(tmp_path)])
    assert rc == 0
    mock_uninstall.assert_called_once_with(unit_dir=tmp_path)


def test_unknown_subcommand_exits_nonzero():
    with pytest.raises(SystemExit) as exc_info:
        main(["frobnicate"])
    assert exc_info.value.code != 0


def test_missing_required_arg_exits_nonzero():
    with pytest.raises(SystemExit) as exc_info:
        main(["logs"])  # missing positional `which`
    assert exc_info.value.code != 0


def test_no_subcommand_returns_nonzero(capsys):
    rc = main([])
    assert rc != 0
    captured = capsys.readouterr()
    assert "usage" in captured.out.lower() or "usage" in captured.err.lower()


def test_systemctl_error_surfaces_as_nonzero_exit_not_traceback(capsys):
    err = SystemctlError(["systemctl", "--user", "start", DAEMON_UNIT], 1, "Unit not found.")
    with patch("dollos.ctl.cli.systemctl.start", side_effect=err):
        rc = main(["start"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "Unit not found" in captured.err
