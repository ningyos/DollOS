"""Tests for systemctl/journalctl --user subprocess wrappers (P1g Task 2).

argv builders are pure functions (no subprocess) — tested directly for
exact argv shape. `_run` is the single subprocess call site — mocked
here so verb wrappers / status / is_active are tested without touching
the real systemd user session.
"""

import subprocess
from unittest.mock import patch

import pytest

from dollos.ctl.systemctl import (
    SystemctlError,
    build_journal_argv,
    build_systemctl_argv,
    daemon_reload,
    disable_now,
    enable_now,
    is_active,
    restart,
    start,
    status,
    stop,
)


def test_build_systemctl_argv_with_unit():
    assert build_systemctl_argv("start", "dollos-daemon.service") == [
        "systemctl",
        "--user",
        "start",
        "dollos-daemon.service",
    ]


def test_build_systemctl_argv_without_unit():
    assert build_systemctl_argv("daemon-reload") == ["systemctl", "--user", "daemon-reload"]


def test_build_journal_argv_follow_true():
    assert build_journal_argv("dollos-bridge.service", follow=True, lines=50) == [
        "journalctl",
        "--user",
        "-u",
        "dollos-bridge.service",
        "-f",
        "-n",
        "50",
    ]


def test_build_journal_argv_follow_false_has_no_dash_f():
    argv = build_journal_argv("dollos-bridge.service", follow=False, lines=50)
    assert "-f" not in argv
    assert argv == ["journalctl", "--user", "-u", "dollos-bridge.service", "-n", "50"]


def test_build_journal_argv_default_lines():
    argv = build_journal_argv("dollos-daemon.service")
    assert argv == ["journalctl", "--user", "-u", "dollos-daemon.service", "-n", "200"]


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_action_verb_raises_on_nonzero_with_stderr_in_message():
    with patch("dollos.ctl.systemctl._run") as mock_run:
        mock_run.return_value = _completed(returncode=1, stderr="Unit not found.")
        with pytest.raises(SystemctlError) as exc_info:
            start("dollos-daemon.service")
        assert "Unit not found." in str(exc_info.value)


def test_start_calls_run_with_correct_argv():
    with patch("dollos.ctl.systemctl._run") as mock_run:
        mock_run.return_value = _completed(returncode=0)
        start("dollos-daemon.service")
        mock_run.assert_called_once_with(
            ["systemctl", "--user", "start", "dollos-daemon.service"]
        )


def test_stop_calls_run_with_correct_argv():
    with patch("dollos.ctl.systemctl._run") as mock_run:
        mock_run.return_value = _completed(returncode=0)
        stop("dollos-daemon.service")
        mock_run.assert_called_once_with(
            ["systemctl", "--user", "stop", "dollos-daemon.service"]
        )


def test_restart_calls_run_with_correct_argv():
    with patch("dollos.ctl.systemctl._run") as mock_run:
        mock_run.return_value = _completed(returncode=0)
        restart("dollos-daemon.service")
        mock_run.assert_called_once_with(
            ["systemctl", "--user", "restart", "dollos-daemon.service"]
        )


def test_daemon_reload_calls_run_with_correct_argv():
    with patch("dollos.ctl.systemctl._run") as mock_run:
        mock_run.return_value = _completed(returncode=0)
        daemon_reload()
        mock_run.assert_called_once_with(["systemctl", "--user", "daemon-reload"])


def test_enable_now_calls_run_with_enable_now_verb():
    with patch("dollos.ctl.systemctl._run") as mock_run:
        mock_run.return_value = _completed(returncode=0)
        enable_now("dollos-daemon.service")
        mock_run.assert_called_once_with(
            ["systemctl", "--user", "enable", "--now", "dollos-daemon.service"]
        )


def test_disable_now_calls_run_with_disable_now_verb():
    with patch("dollos.ctl.systemctl._run") as mock_run:
        mock_run.return_value = _completed(returncode=0)
        disable_now("dollos-daemon.service")
        mock_run.assert_called_once_with(
            ["systemctl", "--user", "disable", "--now", "dollos-daemon.service"]
        )


def test_is_active_true_on_active_stdout():
    with patch("dollos.ctl.systemctl._run") as mock_run:
        mock_run.return_value = _completed(returncode=0, stdout="active\n")
        assert is_active("dollos-daemon.service") is True


def test_is_active_false_on_inactive_stdout_nonzero_rc_no_raise():
    with patch("dollos.ctl.systemctl._run") as mock_run:
        mock_run.return_value = _completed(returncode=3, stdout="inactive\n")
        assert is_active("dollos-daemon.service") is False


def test_status_returns_output_even_when_inactive_nonzero_rc():
    with patch("dollos.ctl.systemctl._run") as mock_run:
        mock_run.return_value = _completed(
            returncode=3, stdout="dollos-daemon.service - DollOS daemon\n   Active: inactive (dead)\n"
        )
        out = status("dollos-daemon.service")
        assert "inactive" in out


def test_status_returns_output_when_active_zero_rc():
    with patch("dollos.ctl.systemctl._run") as mock_run:
        mock_run.return_value = _completed(
            returncode=0, stdout="dollos-daemon.service - DollOS daemon\n   Active: active (running)\n"
        )
        out = status("dollos-daemon.service")
        assert "active (running)" in out
