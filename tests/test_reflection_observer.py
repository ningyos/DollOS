import asyncio
import pytest

from dollos.mind.mind_state import MindState
from dollos.mind.perception_queue import PerceptionQueue
from dollos.mind.reflection_observer import ReflectionObserver


@pytest.mark.asyncio
async def test_fires_after_threshold_iters() -> None:
    state = MindState(iter_count=0)
    queue = PerceptionQueue()
    obs = ReflectionObserver(state=state, queue=queue, threshold=3)
    # Override poll interval for fast test
    obs.POLL_INTERVAL_S = 0.05

    task = asyncio.create_task(obs.run())
    # Initially no fire
    await asyncio.sleep(0.1)
    state.iter_count = 3
    # Wait for next poll
    await asyncio.sleep(0.15)

    obs.shutdown()
    await asyncio.wait_for(task, timeout=1.0)

    perceptions = await queue.drain(timeout_s=0.5)
    refl = [p for p in perceptions if p.kind == "ReflectionMoment"]
    assert len(refl) == 1
    assert refl[0].data["iters_since_last"] == 3


@pytest.mark.asyncio
async def test_does_not_fire_before_threshold() -> None:
    state = MindState(iter_count=0)
    queue = PerceptionQueue()
    obs = ReflectionObserver(state=state, queue=queue, threshold=10)
    obs.POLL_INTERVAL_S = 0.05

    task = asyncio.create_task(obs.run())
    await asyncio.sleep(0.1)
    state.iter_count = 5  # below threshold
    await asyncio.sleep(0.15)

    obs.shutdown()
    await asyncio.wait_for(task, timeout=1.0)

    drained = await queue.drain(timeout_s=0.1)
    refl = [p for p in drained if p.kind == "ReflectionMoment"]
    assert len(refl) == 0


@pytest.mark.asyncio
async def test_resets_counter_after_fire() -> None:
    state = MindState(iter_count=0)
    queue = PerceptionQueue()
    obs = ReflectionObserver(state=state, queue=queue, threshold=3)
    obs.POLL_INTERVAL_S = 0.05

    task = asyncio.create_task(obs.run())
    await asyncio.sleep(0.02)  # let run() initialize _last_fire_at_iter=0
    state.iter_count = 3
    await asyncio.sleep(0.15)  # first fire
    state.iter_count = 5
    await asyncio.sleep(0.15)  # too few since (5-3=2, below 3)
    state.iter_count = 7
    await asyncio.sleep(0.15)  # now should fire again (7-3=4 >= 3)

    obs.shutdown()
    await asyncio.wait_for(task, timeout=1.0)

    drained = await queue.drain(timeout_s=0.5)
    refl = [p for p in drained if p.kind == "ReflectionMoment"]
    assert len(refl) == 2
