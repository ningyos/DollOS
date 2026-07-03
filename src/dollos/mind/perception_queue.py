"""PerceptionQueue — unified asyncio queue for all DollOS event sources."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from dollos.mind.mind_state import Perception

if TYPE_CHECKING:
    from dollos.wal.perception_log import PerceptionWAL


class PerceptionQueue:
    """Asyncio queue that blocks until at least one perception arrives,
    then drains all others already queued.

    Pure event-driven: no idle ticks, no timeouts.
    Supports graceful shutdown via shutdown() so drain() unblocks cleanly.

    When a WAL is provided, every put() also appends to the WAL — so a
    crash before mind_loop drains the queue doesn't lose user input.
    Perceptions arriving from WAL replay (seq already set) skip the
    append to avoid duplicate entries.
    """

    def __init__(self, wal: "PerceptionWAL | None" = None) -> None:
        self._queue: asyncio.Queue[Perception] = asyncio.Queue()
        self._shutdown_event = asyncio.Event()
        self._wal = wal

    def put(self, perception: Perception) -> None:
        """Non-blocking enqueue. Appends to WAL when present and seq is None."""
        if self._wal is not None and perception.seq is None:
            perception.seq = self._wal.append(perception)
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
        # Await cancelled tasks so they can clean up; suppresses per-task
        # "Task was destroyed but it is pending!" warnings (minor lifecycle fix).
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
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

    async def drain_grouped(self, timeout_s: float | None = None) -> list[list[Perception]]:
        """Like drain(), but partition the batch by origin channel so a mixed
        batch yields one bucket per channel (spec §3.1 R2-C1: drain is
        origin-blind; per-origin turn segmentation avoids crosstalk).
        Origin-less perceptions (no data['channel_id']) share one bucket.
        Insertion order within each bucket is preserved; bucket order follows
        first-seen channel."""
        flat = await self.drain(timeout_s)
        if not flat:
            return []
        buckets: dict[str, list[Perception]] = {}
        for p in flat:
            key = (p.data or {}).get("channel_id") or ""
            buckets.setdefault(key, []).append(p)
        return list(buckets.values())
