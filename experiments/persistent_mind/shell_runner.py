"""Minimal async shell runner. Pushes ToolResultArrived back to perception queue.

Note: this is research code. The LLM dispatches shell commands as part of
the architecture being validated. Not for production.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from perceptions import ToolResultArrived


class ShellRunner:
    def __init__(self, perception_queue: asyncio.Queue) -> None:
        self.queue = perception_queue
        self._tasks: dict[str, dict[str, Any]] = {}

    def snapshot(self) -> list[dict]:
        out: list[dict] = []
        for tid, info in self._tasks.items():
            if info.get("done"):
                continue
            out.append({
                "id": tid,
                "tool": "Shell",
                "command": info["command"],
                "started_at": info["started_at"],
                "timeout_s": info["timeout_s"],
            })
        return out

    def dispatch(self, command: str, timeout_s: float = 30.0) -> str:
        dispatch_id = uuid.uuid4().hex[:8]
        self._tasks[dispatch_id] = {
            "command": command,
            "started_at": time.time(),
            "timeout_s": timeout_s,
            "done": False,
        }
        asyncio.create_task(self._run(dispatch_id, command, timeout_s))
        return dispatch_id

    async def _run(self, dispatch_id: str, command: str, timeout_s: float) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
                rc = proc.returncode if proc.returncode is not None else -1
                stdout = stdout_b.decode("utf-8", errors="replace")
                stderr = stderr_b.decode("utf-8", errors="replace")
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                rc = -9
                stdout = ""
                stderr = f"<timeout after {timeout_s}s>"
        except Exception as e:
            rc = -1
            stdout = ""
            stderr = f"<spawn failed: {e!r}>"

        self._tasks[dispatch_id]["done"] = True
        perc = ToolResultArrived(
            tool="Shell",
            command=command,
            returncode=rc,
            stdout=stdout,
            stderr=stderr,
            dispatch_id=dispatch_id,
        )
        await self.queue.put(perc)
