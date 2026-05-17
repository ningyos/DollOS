"""ReflectionObserver — fires ReflectionMoment perceptions threshold-based.

Watches MindState.iter_count. Every N iterations since last fire, pushes
a ReflectionMoment perception into the PerceptionQueue. Doll iterates,
sees the perception, decides what to NoteMemory.
"""
from __future__ import annotations

import asyncio
import logging
import time

from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue

logger = logging.getLogger(__name__)


class ReflectionObserver:
    """Polls MindState.iter_count and fires ReflectionMoment every N iters."""

    DEFAULT_THRESHOLD = 30
    POLL_INTERVAL_S = 5.0  # check every 5s; cheap

    def __init__(
        self,
        *,
        state: MindState,
        queue: PerceptionQueue,
        threshold: int = DEFAULT_THRESHOLD,
    ) -> None:
        self._state = state
        self._queue = queue
        self._threshold = threshold
        self._last_fire_at_iter = 0   # iter_count when we last fired
        self._shutdown = False

    async def run(self) -> None:
        # Initialize from current state (so daemon restart doesn't immediately fire)
        self._last_fire_at_iter = self._state.iter_count
        while not self._shutdown:
            await asyncio.sleep(self.POLL_INTERVAL_S)
            iters_since = self._state.iter_count - self._last_fire_at_iter
            if iters_since >= self._threshold:
                self._queue.put(Perception(
                    kind="ReflectionMoment",
                    t=time.time(),
                    data={"iters_since_last": iters_since},
                ))
                self._last_fire_at_iter = self._state.iter_count
                logger.info("ReflectionMoment fired at iter %d (since=%d)",
                            self._state.iter_count, iters_since)

    def shutdown(self) -> None:
        self._shutdown = True
