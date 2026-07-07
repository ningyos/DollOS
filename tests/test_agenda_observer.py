"""Tests for AgendaObserver — 4-gate self-wake (spec 2026-07-07 §4.1).

Mirrors tests/test_reflection_observer.py's poll+queue driving pattern. Each
gate is exercised alone (the other 3 held passing) to prove it independently
blocks; all-4-pass fires exactly one AgendaMoment; after firing, the throttle
gate blocks an immediate re-fire.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import dollos.mind.agenda_observer as agenda_observer_mod
from dollos.mind.agenda_observer import AgendaObserver
from dollos.mind.mind_state import MindState, OpenLoop
from dollos.mind.perception_queue import PerceptionQueue


def _self_directed_loop(loop_id: str = "loop1") -> OpenLoop:
    return OpenLoop(
        id=loop_id,
        desc="think about the thing",
        opened_at=time.time(),
        self_directed=True,
        trigger="a real conversation",
    )


@pytest.mark.asyncio
async def test_idle_gate_alone_blocks(monkeypatch) -> None:
    """energized + has_loop + throttle-elapsed all pass, but the user just
    spoke (not idle) — must NOT fire."""
    monkeypatch.setattr(agenda_observer_mod, "_POLL_INTERVAL_S", 0.02)
    now = time.time()
    state = MindState(last_user_at=now, energy=1.0, open_loops=[_self_directed_loop()])
    queue = PerceptionQueue()
    obs = AgendaObserver(state=state, queue=queue, energy_idle_threshold_s=600.0)
    obs._last_fire_at = 0.0  # throttle already elapsed

    task = asyncio.create_task(obs.run())
    await asyncio.sleep(0.08)
    obs.shutdown()
    await asyncio.wait_for(task, timeout=1.0)

    drained = await queue.drain(timeout_s=0.1)
    assert [p for p in drained if p.kind == "AgendaMoment"] == []


@pytest.mark.asyncio
async def test_energy_floor_gate_alone_blocks(monkeypatch) -> None:
    """idle + has_loop + throttle-elapsed all pass, but energy <= floor —
    must NOT fire."""
    monkeypatch.setattr(agenda_observer_mod, "_POLL_INTERVAL_S", 0.02)
    now = time.time()
    state = MindState(
        last_user_at=now - 10_000.0,  # long idle
        energy=0.5,  # == floor, not > floor
        open_loops=[_self_directed_loop()],
    )
    queue = PerceptionQueue()
    obs = AgendaObserver(state=state, queue=queue, energy_idle_threshold_s=600.0)
    obs._last_fire_at = 0.0

    task = asyncio.create_task(obs.run())
    await asyncio.sleep(0.08)
    obs.shutdown()
    await asyncio.wait_for(task, timeout=1.0)

    drained = await queue.drain(timeout_s=0.1)
    assert [p for p in drained if p.kind == "AgendaMoment"] == []


@pytest.mark.asyncio
async def test_no_self_directed_loop_gate_alone_blocks(monkeypatch) -> None:
    """idle + energized + throttle-elapsed all pass, but no open loop is
    self_directed — must NOT fire."""
    monkeypatch.setattr(agenda_observer_mod, "_POLL_INTERVAL_S", 0.02)
    now = time.time()
    non_self_directed = OpenLoop(
        id="chore1", desc="owed to the user", opened_at=now, self_directed=False,
    )
    state = MindState(
        last_user_at=now - 10_000.0,
        energy=1.0,
        open_loops=[non_self_directed],
    )
    queue = PerceptionQueue()
    obs = AgendaObserver(state=state, queue=queue, energy_idle_threshold_s=600.0)
    obs._last_fire_at = 0.0

    task = asyncio.create_task(obs.run())
    await asyncio.sleep(0.08)
    obs.shutdown()
    await asyncio.wait_for(task, timeout=1.0)

    drained = await queue.drain(timeout_s=0.1)
    assert [p for p in drained if p.kind == "AgendaMoment"] == []


@pytest.mark.asyncio
async def test_throttle_gate_alone_blocks(monkeypatch) -> None:
    """idle + energized + has_loop all pass, but the throttle window hasn't
    elapsed since the last fire — must NOT fire."""
    monkeypatch.setattr(agenda_observer_mod, "_POLL_INTERVAL_S", 0.02)
    monkeypatch.setattr(agenda_observer_mod, "_AGENDA_MIN_INTERVAL_S", 10_000.0)
    now = time.time()
    state = MindState(
        last_user_at=now - 10_000.0,
        energy=1.0,
        open_loops=[_self_directed_loop()],
    )
    queue = PerceptionQueue()
    obs = AgendaObserver(state=state, queue=queue, energy_idle_threshold_s=600.0)
    obs._last_fire_at = now  # just "fired" — throttle window wide open

    task = asyncio.create_task(obs.run())
    await asyncio.sleep(0.08)
    obs.shutdown()
    await asyncio.wait_for(task, timeout=1.0)

    drained = await queue.drain(timeout_s=0.1)
    assert [p for p in drained if p.kind == "AgendaMoment"] == []


@pytest.mark.asyncio
async def test_all_four_gates_pass_fires_once(monkeypatch) -> None:
    """``run()`` resets ``_last_fire_at`` to "now" the instant it boots
    (mirrors ReflectionObserver — no immediate fire on start), so to
    exercise a real fire we let it boot, then force the throttle clock back
    to "long ago" (0.0) from outside — simulating "an old last fire" without
    racing real wall-clock deltas against ``_AGENDA_MIN_INTERVAL_S``."""
    monkeypatch.setattr(agenda_observer_mod, "_POLL_INTERVAL_S", 0.03)
    now = time.time()
    state = MindState(
        last_user_at=now - 10_000.0,
        energy=1.0,
        open_loops=[_self_directed_loop()],
    )
    queue = PerceptionQueue()
    obs = AgendaObserver(state=state, queue=queue, energy_idle_threshold_s=600.0)

    task = asyncio.create_task(obs.run())
    await asyncio.sleep(0.01)  # let run() perform its boot reset
    obs._last_fire_at = 0.0    # force throttle open for the next poll
    await asyncio.sleep(0.08)  # one poll cycle fires; _last_fire_at is now recent again
    obs.shutdown()
    await asyncio.wait_for(task, timeout=1.0)

    drained = await queue.drain(timeout_s=0.1)
    fired = [p for p in drained if p.kind == "AgendaMoment"]
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_throttle_blocks_immediate_refire_after_firing(monkeypatch) -> None:
    """After a forced fire, the throttle clock resets to "now" — many rapid
    poll cycles afterward (default ``_AGENDA_MIN_INTERVAL_S``, unmodified)
    must NOT produce a second fire."""
    monkeypatch.setattr(agenda_observer_mod, "_POLL_INTERVAL_S", 0.01)
    now = time.time()
    state = MindState(
        last_user_at=now - 10_000.0,
        energy=1.0,
        open_loops=[_self_directed_loop()],
    )
    queue = PerceptionQueue()
    obs = AgendaObserver(state=state, queue=queue, energy_idle_threshold_s=600.0)

    task = asyncio.create_task(obs.run())
    await asyncio.sleep(0.005)  # let run() perform its boot reset
    obs._last_fire_at = 0.0     # force the one-time fire
    # Let ~15 poll cycles elapse at the default (~7 min) throttle — none of
    # them should reach a second fire.
    await asyncio.sleep(0.15)
    obs.shutdown()
    await asyncio.wait_for(task, timeout=1.0)

    drained = await queue.drain(timeout_s=0.1)
    fired = [p for p in drained if p.kind == "AgendaMoment"]
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_boot_does_not_fire_immediately(monkeypatch) -> None:
    """Mirrors ReflectionObserver: run() initializes _last_fire_at from
    "now" at boot, so even if the other 3 gates already pass, the very
    first poll right after boot doesn't fire (throttle clock starts fresh)."""
    monkeypatch.setattr(agenda_observer_mod, "_POLL_INTERVAL_S", 0.02)
    monkeypatch.setattr(agenda_observer_mod, "_AGENDA_MIN_INTERVAL_S", 5.0)
    now = time.time()
    state = MindState(
        last_user_at=now - 10_000.0,
        energy=1.0,
        open_loops=[_self_directed_loop()],
    )
    queue = PerceptionQueue()
    obs = AgendaObserver(state=state, queue=queue, energy_idle_threshold_s=600.0)
    # Do NOT pre-seed obs._last_fire_at — let run() set it at boot.

    task = asyncio.create_task(obs.run())
    await asyncio.sleep(0.08)
    obs.shutdown()
    await asyncio.wait_for(task, timeout=1.0)

    drained = await queue.drain(timeout_s=0.1)
    assert [p for p in drained if p.kind == "AgendaMoment"] == []
