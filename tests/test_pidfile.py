import os
from pathlib import Path

import pytest

from dollos.wal.pidfile import PidFile, RestartKind


def test_first_start_is_cold(tmp_path):
    pf = PidFile(tmp_path / "daemon.pid")
    kind = pf.acquire()
    assert kind == RestartKind.COLD
    assert pf.path.read_text() == str(os.getpid())


def test_clean_release_then_restart_is_cold(tmp_path):
    pf1 = PidFile(tmp_path / "daemon.pid")
    pf1.acquire()
    pf1.release()
    assert not pf1.path.exists()

    pf2 = PidFile(tmp_path / "daemon.pid")
    kind = pf2.acquire()
    assert kind == RestartKind.COLD


def test_dirty_restart_detected_when_pid_gone(tmp_path):
    """If the previous pid is no longer running and the file wasn't deleted, it's dirty."""
    path = tmp_path / "daemon.pid"
    path.write_text("99999")  # almost certainly not a running pid
    pf = PidFile(path)
    kind = pf.acquire()
    assert kind == RestartKind.DIRTY


def test_dirty_restart_replaces_pid(tmp_path):
    path = tmp_path / "daemon.pid"
    path.write_text("99999")
    pf = PidFile(path)
    pf.acquire()
    assert pf.path.read_text() == str(os.getpid())


def test_corrupt_pidfile_treated_as_dirty(tmp_path):
    path = tmp_path / "daemon.pid"
    path.write_text("not-a-number")
    pf = PidFile(path)
    kind = pf.acquire()
    assert kind == RestartKind.DIRTY


def test_another_running_daemon_raises(tmp_path):
    """If pidfile names a pid that IS running (e.g. our own pid), refuse to start."""
    path = tmp_path / "daemon.pid"
    path.write_text(str(os.getpid()))  # our own pid is definitely alive
    pf = PidFile(path)
    with pytest.raises(RuntimeError):
        pf.acquire()
