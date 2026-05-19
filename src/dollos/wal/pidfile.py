"""PidFile — daemon-running marker for dirty restart detection."""
from __future__ import annotations

import enum
import os
from pathlib import Path


class RestartKind(enum.Enum):
    COLD = "cold"       # first start or clean previous shutdown
    DIRTY = "dirty"     # previous run crashed (pidfile left, pid no longer running)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


class PidFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def acquire(self) -> RestartKind:
        """Write our pid; return COLD or DIRTY based on previous state.

        Raises RuntimeError if the pidfile names a pid that is still running
        and is not us — refuses to start a second daemon.
        """
        kind = RestartKind.COLD
        if self.path.exists():
            prev = self.path.read_text().strip()
            try:
                prev_pid = int(prev)
                if _pid_alive(prev_pid):
                    raise RuntimeError(
                        f"another DollOS daemon appears to be running (pid={prev_pid})"
                    )
                kind = RestartKind.DIRTY
            except ValueError:
                kind = RestartKind.DIRTY  # corrupt file = treat as dirty
        self.path.write_text(str(os.getpid()))
        return kind

    def release(self) -> None:
        """Delete the pidfile on clean shutdown. Idempotent."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
