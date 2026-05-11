"""ShellRunner — spawn shell subprocesses; fire ShellResultEvent on completion.

Mirrors SubagentRunner's fire-and-forget pattern. Doll's `Shell` tool calls
into this runner and returns immediately; the runner watches the proc and
emits a `ShellResultEvent` via `dispatch_fn` when the proc exits, times
out, or errors. There is no Doll-callable wait/cancel — "wait" is just
Doll keeping her cascade alive, "cancel" only happens on daemon shutdown.

Lifecycle:
    Shell.run → ctx.shell_runner.spawn(command, timeout_s, response_sink)
                → asyncio.create_task(_run(...))
                       → spawn proc, await communicate() with timeout
                       → dispatch ShellResultEvent
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Callable
from pathlib import Path

from dollos.events import RawEvent, ShellResultEvent
from dollos.ipc.messages import ServerMessage

logger = logging.getLogger(__name__)


SHELL_OUTPUT_MAX_CHARS = 8000


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    half = cap // 2
    head = text[:half]
    tail = text[-half:]
    dropped = len(text) - 2 * half
    return f"{head}\n...[truncated {dropped} chars]...\n{tail}"


class ShellRunner:
    """Spawn-and-track set of background shell subprocesses.

    Dispatch sink is wired post-build via set_dispatch_fn (see kernel.py).
    """

    def __init__(
        self,
        *,
        cwd: Path,
        dispatch_fn: Callable[[RawEvent], None] | None = None,
    ) -> None:
        self._cwd = cwd
        self._dispatch_fn = dispatch_fn
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    def set_dispatch_fn(self, fn: Callable[[RawEvent], None]) -> None:
        self._dispatch_fn = fn

    def spawn(
        self,
        *,
        command: str,
        timeout_s: int,
        response_sink: asyncio.Queue[ServerMessage | None] | None,
    ) -> None:
        """Schedule a shell subprocess. Returns immediately."""
        if self._stopping:
            logger.warning("shell spawn ignored: runner stopping")
            return
        coro = self._run(command, timeout_s, response_sink)
        t = asyncio.create_task(coro, name=f"shell-{command[:20]!r}")
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        self._stopping = True
        if not self._tasks:
            return
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _run(
        self,
        command: str,
        timeout_s: int,
        response_sink: asyncio.Queue[ServerMessage | None] | None,
    ) -> None:
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self._cwd),
                start_new_session=True,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
                self._fire(ShellResultEvent(
                    command=command,
                    status="timeout",
                    exit_code=None,
                    output=f"[timed out after {timeout_s}s]",
                    response_sink=response_sink,
                ))
                return
            output = _truncate(
                (stdout or b"").decode("utf-8", errors="replace"),
                SHELL_OUTPUT_MAX_CHARS,
            )
            status = "ok" if proc.returncode == 0 else "nonzero"
            self._fire(ShellResultEvent(
                command=command,
                status=status,
                exit_code=proc.returncode,
                output=output,
                response_sink=response_sink,
            ))
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    await proc.wait()
                except Exception:
                    pass
            raise
        except Exception as e:
            logger.exception("ShellRunner._run unexpected error")
            self._fire(ShellResultEvent(
                command=command,
                status="error",
                exit_code=None,
                output=f"[runner error: {e}]",
                response_sink=response_sink,
            ))

    def _fire(self, ev: ShellResultEvent) -> None:
        if self._dispatch_fn is None:
            logger.error(
                "ShellResultEvent dropped: dispatch_fn not set "
                "(command=%r status=%s)", ev.command, ev.status,
            )
            return
        try:
            self._dispatch_fn(ev)
        except Exception:
            logger.exception("dispatch_fn raised on ShellResultEvent")
