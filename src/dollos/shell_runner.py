"""ShellRunner — spawn shell subprocesses; enqueue Perception on completion.

Fire-and-forget pattern. Doll's `Shell` tool calls into this runner and
returns immediately; the runner watches the proc and enqueues a Perception
into the PerceptionQueue when the proc exits, times out, or errors.
There is no Doll-callable wait/cancel — "wait" is just Doll keeping her
cascade alive, "cancel" only happens on daemon shutdown.

Lifecycle:
    Shell.run → ctx.shell_runner.spawn(command, timeout_s)
                → asyncio.create_task(_run(...))
                       → spawn proc, await communicate() with timeout
                       → perception_queue.put(Perception("ToolResultArrived", ...))
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from dollos.tool_outputs import ToolOutputStore

if TYPE_CHECKING:
    from dollos.mind.perception_queue import PerceptionQueue

logger = logging.getLogger(__name__)


SHELL_PREVIEW_LINES = 10


class ShellRunner:
    """Spawn-and-track set of background shell subprocesses.

    PerceptionQueue is wired post-build via set_perception_queue (see kernel.py).
    """

    def __init__(
        self,
        *,
        cwd: Path,
        perception_queue: "PerceptionQueue | None" = None,
        tool_output_store: ToolOutputStore,
    ) -> None:
        self._cwd = cwd
        self._perception_queue = perception_queue
        self._tool_output_store = tool_output_store
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    def set_perception_queue(self, queue: "PerceptionQueue") -> None:
        self._perception_queue = queue

    def spawn(
        self,
        *,
        command: str,
        timeout_s: int,
        response_sink=None,  # kept for call-site compatibility; ignored
    ) -> None:
        """Schedule a shell subprocess. Returns immediately."""
        if self._stopping:
            logger.warning("shell spawn ignored: runner stopping")
            return
        task_id = f"shell-{uuid.uuid4().hex[:8]}"
        coro = self._run(command, timeout_s, task_id)
        t = asyncio.create_task(coro, name=task_id)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)


    async def stop(self) -> None:
        self._stopping = True
        if not self._tasks:
            return
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _run(self, command: str, timeout_s: int, task_id: str) -> None:
        from dollos.mind.mind_state import Perception
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
                timeout_msg = f"[timed out after {timeout_s}s]"
                output_id: str = self._tool_output_store.write(timeout_msg)
                self._enqueue(Perception(
                    kind="ToolResultArrived",
                    t=time.time(),
                    data={
                        "tool": "Shell",
                        "task_id": task_id,
                        "status": "timeout",
                        "summary": f"shell timed out after {timeout_s}s",
                        "output_id": output_id,
                        "line_count": 1,
                        "command": command,
                    },
                ))
                return
            full_output = (stdout or b"").decode("utf-8", errors="replace")
            all_lines = full_output.splitlines()
            line_count = len(all_lines)
            preview = "\n".join(all_lines[:SHELL_PREVIEW_LINES])
            output_id = self._tool_output_store.write(full_output)
            status = "ok" if proc.returncode == 0 else "nonzero"
            self._enqueue(Perception(
                kind="ToolResultArrived",
                t=time.time(),
                data={
                    "tool": "Shell",
                    "task_id": f"shell-{command[:20]}",
                    "status": status,
                    "summary": (
                        f"exit {proc.returncode}: {preview[:80]}"
                        if all_lines else f"exit {proc.returncode}: (empty)"
                    ),
                    "output_id": output_id,
                    "line_count": line_count,
                    "command": command,
                    "exit_code": proc.returncode,
                },
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
            self._enqueue(Perception(
                kind="ToolResultArrived",
                t=time.time(),
                data={
                    "tool": "Shell",
                    "task_id": f"shell-{command[:20]}",
                    "status": "error",
                    "summary": f"runner error: {e}",
                    "output_id": None,
                    "line_count": 1,
                    "command": command,
                },
            ))

    def _enqueue(self, perception: "Perception") -> None:
        if self._perception_queue is None:
            logger.error(
                "ShellResult dropped: perception_queue not set "
                "(command=%r)", perception.data.get("command", "?"),
            )
            return
        try:
            self._perception_queue.put(perception)
        except Exception:
            logger.exception("perception_queue.put raised on shell result")
