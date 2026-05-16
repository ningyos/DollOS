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

    async def drain(self) -> list[Perception]:
        """Block until the first perception arrives, then drain any others
        already queued. Returns at least one perception, or empty list if
        shutdown() was called while waiting."""
        # Race between queue.get() and shutdown signal.
        get_task = asyncio.ensure_future(self._queue.get())
        shutdown_task = asyncio.ensure_future(self._shutdown_event.wait())
        done, pending = await asyncio.wait(
            {get_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if shutdown_task in done:
            # Shutdown signaled — cancel queue.get and return empty
            get_task.cancel()
            return []
        first = get_task.result()
        out = [first]
        while not self._queue.empty():
            out.append(self._queue.get_nowait())
        return out
