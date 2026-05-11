"""MonitorRunner — spawn long-running shell commands; fire events per matched line.

Fire-and-forget pattern (sibling of ShellRunner / SubagentRunner). Doll
calls SpawnMonitor → MonitorRunner.spawn returns an id, kicks off an
asyncio.Task that reads proc.stdout line by line. Each line is
optionally regex-filtered, then rate-limited; surviving lines fire a
MonitorTriggeredEvent via dispatch_fn. Process exit (natural / removed /
error) always fires exactly one MonitorExitedEvent.

State surfaces to dispatcher via `active_state()` for the
`[Active monitors]` perception block; only suppression counters need to
be visible (rate-limit window, hits suppressed in last window).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dollos.events import (
    MonitorExitedEvent,
    MonitorTriggeredEvent,
    RawEvent,
)
from dollos.ipc.messages import ServerMessage

logger = logging.getLogger(__name__)


@dataclass
class ActiveMonitor:
    monitor_id: str
    command: str
    match_regex: str | None  # raw pattern string for display
    rate_limit_s: int
    started_at: datetime
    response_sink: asyncio.Queue[ServerMessage | None] | None
    # Compiled regex (None if match_regex is None).
    _compiled: re.Pattern[str] | None = None
    # Rate-limit state. window_start is when the last fire happened; None
    # means no fire yet.
    window_start: datetime | None = None
    suppressed_in_window: int = 0
    total_matched: int = 0  # total lines that passed regex (incl. suppressed)
    # Lifecycle.
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None
    remove_requested: bool = False


class MonitorRunner:
    """Spawn-and-track set of long-running monitor commands.

    Built before the dispatcher (chicken-and-egg: SpawnMonitor.run needs
    the runner; the runner needs to dispatch events into the dispatcher).
    `set_dispatch_fn` wires the sink after dispatcher build.
    """

    def __init__(
        self,
        *,
        cwd: Path,
        dispatch_fn: Callable[[RawEvent], None] | None = None,
    ) -> None:
        self._cwd = cwd
        self._dispatch_fn = dispatch_fn
        self._active: dict[str, ActiveMonitor] = {}
        self._counter = 0
        self._stopping = False

    def set_dispatch_fn(self, fn: Callable[[RawEvent], None]) -> None:
        self._dispatch_fn = fn

    def spawn(
        self,
        *,
        command: str,
        match_regex: str | None,
        rate_limit_s: int,
        response_sink: asyncio.Queue[ServerMessage | None] | None,
    ) -> str:
        """Spawn a monitor. Returns monitor_id (e.g., 'mon-1').

        Raises re.error if match_regex is invalid.
        """
        if self._stopping:
            logger.warning("monitor spawn ignored: runner stopping")
            return ""
        compiled = re.compile(match_regex) if match_regex else None
        self._counter += 1
        monitor_id = f"mon-{self._counter}"
        mon = ActiveMonitor(
            monitor_id=monitor_id,
            command=command,
            match_regex=match_regex,
            rate_limit_s=rate_limit_s,
            started_at=datetime.now(),
            response_sink=response_sink,
            _compiled=compiled,
        )
        mon.task = asyncio.create_task(
            self._watch(mon), name=f"monitor-{monitor_id}"
        )
        self._active[monitor_id] = mon
        return monitor_id

    async def remove(self, monitor_id: str) -> bool:
        """Kill the monitor's process and remove from active set.

        Returns True if the monitor existed, False otherwise. The
        watcher task will fire MonitorExitedEvent with status='removed'
        and then exit; remove() does not await that — caller can poll
        active_state() if needed.
        """
        mon = self._active.get(monitor_id)
        if mon is None:
            return False
        mon.remove_requested = True
        if mon.proc is not None and mon.proc.returncode is None:
            try:
                os.killpg(os.getpgid(mon.proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        return True

    def active_state(self) -> list[dict[str, Any]]:
        """Snapshot for the dispatcher's [Active monitors] block."""
        out: list[dict[str, Any]] = []
        for mon in self._active.values():
            out.append({
                "monitor_id": mon.monitor_id,
                "command": mon.command,
                "match_regex": mon.match_regex,
                "rate_limit_s": mon.rate_limit_s,
                "suppressed_in_window": mon.suppressed_in_window,
            })
        return out

    async def stop(self) -> None:
        self._stopping = True
        for mon in list(self._active.values()):
            if mon.proc is not None and mon.proc.returncode is None:
                try:
                    os.killpg(os.getpgid(mon.proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if mon.task is not None and not mon.task.done():
                mon.task.cancel()
        tasks = [m.task for m in self._active.values() if m.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()

    def _fire(self, ev: RawEvent) -> None:
        if self._dispatch_fn is None:
            logger.error(
                "monitor event dropped: dispatch_fn not set "
                "(type=%s)", type(ev).__name__,
            )
            return
        try:
            self._dispatch_fn(ev)
        except Exception:
            logger.exception("dispatch_fn raised on monitor event")

    async def _watch(self, mon: ActiveMonitor) -> None:
        status: str = "natural"
        exit_code: int | None = None
        try:
            mon.proc = await asyncio.create_subprocess_shell(
                mon.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self._cwd),
                start_new_session=True,
            )
            assert mon.proc.stdout is not None
            while True:
                raw = await mon.proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if mon._compiled is not None and not mon._compiled.search(line):
                    continue
                mon.total_matched += 1
                self._consider_fire(mon, line)
            await mon.proc.wait()
            exit_code = mon.proc.returncode
            if mon.remove_requested:
                status = "removed"
        except asyncio.CancelledError:
            if mon.proc is not None and mon.proc.returncode is None:
                try:
                    os.killpg(os.getpgid(mon.proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            raise
        except Exception as e:
            logger.exception("MonitorRunner._watch unexpected error")
            self._fire(MonitorExitedEvent(
                monitor_id=mon.monitor_id,
                command=mon.command,
                status="error",
                exit_code=None,
                total_matched=mon.total_matched,
                response_sink=mon.response_sink,
            ))
            self._active.pop(mon.monitor_id, None)
            return
        # Natural / removed path — fire exit + drop from active set.
        self._fire(MonitorExitedEvent(
            monitor_id=mon.monitor_id,
            command=mon.command,
            status=status,
            exit_code=exit_code,
            total_matched=mon.total_matched,
            response_sink=mon.response_sink,
        ))
        self._active.pop(mon.monitor_id, None)

    def _consider_fire(self, mon: ActiveMonitor, line: str) -> None:
        now = datetime.now()
        rate = mon.rate_limit_s
        if rate <= 0:
            self._fire(MonitorTriggeredEvent(
                monitor_id=mon.monitor_id,
                command=mon.command,
                line=line,
                suppressed_count=0,
                response_sink=mon.response_sink,
            ))
            return
        if mon.window_start is None:
            mon.window_start = now
            mon.suppressed_in_window = 0
            self._fire(MonitorTriggeredEvent(
                monitor_id=mon.monitor_id,
                command=mon.command,
                line=line,
                suppressed_count=0,
                response_sink=mon.response_sink,
            ))
            return
        elapsed = (now - mon.window_start).total_seconds()
        if elapsed >= rate:
            fired_suppressed = mon.suppressed_in_window
            mon.window_start = now
            mon.suppressed_in_window = 0
            self._fire(MonitorTriggeredEvent(
                monitor_id=mon.monitor_id,
                command=mon.command,
                line=line,
                suppressed_count=fired_suppressed,
                response_sink=mon.response_sink,
            ))
            return
        mon.suppressed_in_window += 1
