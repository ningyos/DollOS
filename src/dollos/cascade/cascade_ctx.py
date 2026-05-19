"""CascadeCtx — owns the cancel signal for one cascade run."""
from __future__ import annotations

import asyncio


class CascadeCtx:
    """Single-cascade cancel signal.

    Created by mind_loop at the start of an iter; held until the iter
    returns. External callers (kernel on new TextInput) set the cancel
    via mind_loop.cancel_current_cascade() — propagates through this
    Event so the in-flight LLM stream / dispatch loop returns cleanly
    at the next checkpoint.
    """

    def __init__(self) -> None:
        self._cancel_event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def cancel(self) -> None:
        self._cancel_event.set()

    async def wait_cancelled(self) -> None:
        # Not used by Plan 3; reserved for future cancel-aware awaits inside cascade.
        await self._cancel_event.wait()
