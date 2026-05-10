"""ProcessRegistry — kernel-level tracker for async Shell subprocesses.

Phase 2 of the 4-phase async-tool plan
(`docs/superpowers/plans/2026-05-10-async-shell.md`).

Hands out monotonic ``sh-N`` handles. ``Shell.run`` registers a freshly
spawned process and returns the handle to Doll. ``Monitor(handle)``
later looks the process up, awaits its output, then removes it.

The registry is in-memory only; daemon shutdown kills any stragglers
via :py:meth:`ProcessRegistry.shutdown`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class ManagedProcess:
    """One subprocess being tracked by the registry."""

    handle: str
    proc: asyncio.subprocess.Process
    command: str  # original shell command, kept for debug


class ProcessRegistry:
    """Tracks running subprocesses for async Shell + Monitor.

    Handles are monotonic strings ``sh-1``, ``sh-2``, ... — the counter
    never resets within a single daemon lifetime. Removed after Monitor
    consumes the process. Stale handles (timeout / never monitored) are
    cleaned up by ``shutdown`` (and by Cancel in Phase 3).
    """

    def __init__(self) -> None:
        self._processes: dict[str, ManagedProcess] = {}
        self._counter = 0

    def register(
        self, proc: asyncio.subprocess.Process, command: str
    ) -> str:
        self._counter += 1
        handle = f"sh-{self._counter}"
        self._processes[handle] = ManagedProcess(
            handle=handle, proc=proc, command=command
        )
        return handle

    def get(self, handle: str) -> ManagedProcess | None:
        return self._processes.get(handle)

    def remove(self, handle: str) -> None:
        self._processes.pop(handle, None)

    async def shutdown(self) -> None:
        """Kill any still-running processes on daemon shutdown.

        Best-effort: ignores per-process errors (already exited, signal
        delivery race, etc.). Always clears the dict at the end so the
        registry is usable again if reused (tests).
        """
        for mp in list(self._processes.values()):
            if mp.proc.returncode is None:
                try:
                    mp.proc.kill()
                    await mp.proc.wait()
                except Exception:
                    pass
        self._processes.clear()
