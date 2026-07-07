"""AgendaObserver — fires AgendaMoment perceptions (self-directed agenda
self-wake), 4-gate + throttle.

Mirrors reflection_observer.py: a poller that watches MindState and pushes an
origin-less internal perception into the PerceptionQueue when its gate
condition holds. See spec docs/superpowers/specs/2026-07-07-self-directed-
agenda-design.md §4.1.

ALL 4 gates must pass for a fire:
  1. idle     — now - state.last_user_at > energy_idle_threshold_s (the
                energy system's existing idle threshold, ~600s).
  2. energy   — state.energy > _AGENDA_ENERGY_FLOOR. Reserves at least half
                of full energy for reactive turns — autonomous pursuit must
                never be able to run her down to unresponsive (spec §4.2:
                "energy floor only gates autonomous turns, never reactive").
  3. has_loop — any open loop is self_directed (nothing to silently invent;
                genesis happens only on reflection/reactive turns, never here
                — R1-I3, keeps the gate from self-perpetuating).
  4. throttle — at least _AGENDA_MIN_INTERVAL_S since the last fire. THIS is
                the primary rhythm bound (R1-I3), not energy: a pure-thinking
                AgendaMoment turn with no tool call never deducts energy
                (mind_loop.py's cost_per_turn deduction only fires when the
                turn produced speech or a tool call), so gate #2 alone would
                stay open indefinitely once idle — throttle is what keeps
                this to "advances a step occasionally," not "spins while
                idle."
"""
from __future__ import annotations

import asyncio
import logging
import time

from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 30.0
_AGENDA_ENERGY_FLOOR = 0.5
_AGENDA_MIN_INTERVAL_S = 420.0  # ~7 min — primary rhythm bound (R1-I3)


class AgendaObserver:
    """Polls MindState and fires AgendaMoment when all 4 gates pass."""

    def __init__(
        self,
        *,
        state: MindState,
        queue: PerceptionQueue,
        energy_idle_threshold_s: float,
    ) -> None:
        self._state = state
        self._queue = queue
        self._idle_threshold = energy_idle_threshold_s
        self._last_fire_at = 0.0
        self._shutdown = False

    async def run(self) -> None:
        # Initialize from "now" (mirrors ReflectionObserver) so a daemon
        # restart doesn't immediately fire — the throttle clock starts fresh
        # at boot rather than treating a cold start as "overdue."
        self._last_fire_at = time.time()
        while not self._shutdown:
            await asyncio.sleep(_POLL_INTERVAL_S)
            now = time.time()
            idle = (now - self._state.last_user_at) > self._idle_threshold
            energized = self._state.energy > _AGENDA_ENERGY_FLOOR
            has_loop = any(l.self_directed for l in self._state.open_loops)
            throttle_elapsed = (now - self._last_fire_at) >= _AGENDA_MIN_INTERVAL_S
            if idle and energized and has_loop and throttle_elapsed:
                self._queue.put(Perception(kind="AgendaMoment", t=now, data={}))
                self._last_fire_at = now
                logger.info("AgendaMoment fired at t=%.0f", now)

    def shutdown(self) -> None:
        self._shutdown = True
