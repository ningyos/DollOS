"""BatchAccumulator — coalesce same-channel items within a time window before
enqueue (spec 2026-07-03 §3.1 I1: PerceptionQueue.drain has no post-first
accumulation window). Pure asyncio timing; caller supplies enqueue()."""
from __future__ import annotations

import asyncio
from typing import Callable


class BatchAccumulator:
    def __init__(self, enqueue: Callable[[list[dict]], None], window_s: float) -> None:
        self._enqueue = enqueue
        self._window_s = window_s
        self._pending: dict[str, list[dict]] = {}
        self._timers: dict[str, asyncio.Task] = {}

    async def add(self, channel_id: str, item: dict) -> None:
        if channel_id not in self._pending:
            self._pending[channel_id] = []
            self._timers[channel_id] = asyncio.ensure_future(self._fire_after(channel_id))
        self._pending[channel_id].append(item)

    async def _fire_after(self, channel_id: str) -> None:
        try:
            await asyncio.sleep(self._window_s)
        except asyncio.CancelledError:
            return
        self._flush(channel_id)

    def _flush(self, channel_id: str) -> None:
        items = self._pending.pop(channel_id, None)
        self._timers.pop(channel_id, None)
        if items:
            self._enqueue(items)

    async def flush_all(self) -> None:
        for t in list(self._timers.values()):
            t.cancel()
        for cid in list(self._pending):
            self._flush(cid)
