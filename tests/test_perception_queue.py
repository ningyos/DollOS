import asyncio
import pytest

from dollos.mind.perception_queue import PerceptionQueue
from dollos.mind.mind_state import Perception


@pytest.mark.asyncio
async def test_put_then_drain_returns_perception() -> None:
    q = PerceptionQueue()
    p = Perception(kind="UserSpoke", t=1.0, data={"text": "hi"})
    q.put(p)
    drained = await q.drain(timeout_s=1.0)
    assert len(drained) == 1
    assert drained[0].kind == "UserSpoke"


@pytest.mark.asyncio
async def test_drain_timeout_yields_idle_tick() -> None:
    q = PerceptionQueue()
    drained = await q.drain(timeout_s=0.1)
    assert len(drained) == 1
    assert drained[0].kind == "IdleTick"


@pytest.mark.asyncio
async def test_drain_returns_all_pending() -> None:
    q = PerceptionQueue()
    q.put(Perception(kind="UserSpoke", t=1.0, data={}))
    q.put(Perception(kind="ToolResultArrived", t=2.0, data={}))
    drained = await q.drain(timeout_s=1.0)
    assert len(drained) == 2
    assert drained[0].kind == "UserSpoke"
    assert drained[1].kind == "ToolResultArrived"


@pytest.mark.asyncio
async def test_drain_does_not_block_when_perceptions_available() -> None:
    import time
    q = PerceptionQueue()
    q.put(Perception(kind="UserSpoke", t=1.0, data={}))
    start = time.time()
    drained = await q.drain(timeout_s=5.0)
    elapsed = time.time() - start
    assert len(drained) == 1
    assert elapsed < 0.1
