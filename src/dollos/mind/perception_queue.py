"""PerceptionQueue — unified asyncio queue for all DollOS event sources."""
from __future__ import annotations

import asyncio
import time

from dollos.mind.mind_state import Perception


class PerceptionQueue:
    """Asyncio queue that drains all pending perceptions, or yields an
    IdleTick perception after a timeout."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Perception] = asyncio.Queue()

    def put(self, perception: Perception) -> None:
        """Non-blocking enqueue."""
        self._queue.put_nowait(perception)

    async def drain(self, *, timeout_s: float) -> list[Perception]:
        """Wait up to timeout_s for at least one perception, then drain
        any others already queued. Returns [IdleTick] if timeout fires."""
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return [Perception(kind="IdleTick", t=time.time(), data={})]
        out = [first]
        while not self._queue.empty():
            out.append(self._queue.get_nowait())
        return out
