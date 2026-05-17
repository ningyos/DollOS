"""PerceptionQueue — unified asyncio queue for all DollOS event sources."""
from __future__ import annotations

import asyncio

from dollos.mind.mind_state import Perception


class PerceptionQueue:
    """Asyncio queue that blocks until at least one perception arrives,
    then drains all others already queued.

    Pure event-driven: no idle ticks, no timeouts.
    Supports graceful shutdown via shutdown() so drain() unblocks cleanly.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Perception] = asyncio.Queue()
        self._shutdown_event = asyncio.Event()

    def put(self, perception: Perception) -> None:
        """Non-blocking enqueue."""
        self._queue.put_nowait(perception)

    def shutdown(self) -> None:
        """Signal drain() to unblock and return empty list on next call."""
        self._shutdown_event.set()

    async def drain(self, timeout_s: float | None = None) -> list[Perception]:
        """Block until the first perception arrives, then drain any others
        already queued. Returns at least one perception, or empty list if
        shutdown() was called while waiting or timeout_s elapsed."""
        # Race between queue.get(), shutdown signal, and optional timeout.
        get_task = asyncio.ensure_future(self._queue.get())
        shutdown_task = asyncio.ensure_future(self._shutdown_event.wait())
        wait_tasks: set = {get_task, shutdown_task}
        timeout_task: asyncio.Task | None = None
        if timeout_s is not None:
            timeout_task = asyncio.ensure_future(asyncio.sleep(timeout_s))
            wait_tasks.add(timeout_task)
        done, pending = await asyncio.wait(
            wait_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if shutdown_task in done or (timeout_task is not None and timeout_task in done and get_task not in done):
            # Shutdown signaled or timeout elapsed before any perception
            get_task.cancel()
            return []
        if get_task not in done:
            return []
        first = get_task.result()
        out = [first]
        while not self._queue.empty():
            out.append(self._queue.get_nowait())
        return out
