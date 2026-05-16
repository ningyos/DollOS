import asyncio
import pytest

from dollos.mind.perception_queue import PerceptionQueue
from dollos.mind.mind_state import Perception


@pytest.mark.asyncio
async def test_put_then_drain_returns_perception() -> None:
    q = PerceptionQueue()
    p = Perception(kind="UserSpoke", t=1.0, data={"text": "hi"})
    q.put(p)
    drained = await q.drain()
    assert len(drained) == 1
    assert drained[0].kind == "UserSpoke"


@pytest.mark.asyncio
async def test_drain_returns_all_pending() -> None:
    q = PerceptionQueue()
    q.put(Perception(kind="UserSpoke", t=1.0, data={}))
    q.put(Perception(kind="ToolResultArrived", t=2.0, data={}))
    drained = await q.drain()
    assert len(drained) == 2
    assert drained[0].kind == "UserSpoke"
    assert drained[1].kind == "ToolResultArrived"


@pytest.mark.asyncio
async def test_drain_does_not_block_when_perceptions_available() -> None:
    import time
    q = PerceptionQueue()
    q.put(Perception(kind="UserSpoke", t=1.0, data={}))
    start = time.time()
    drained = await q.drain()
    elapsed = time.time() - start
    assert len(drained) == 1
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_drain_blocks_until_perception_injected() -> None:
    """drain() must block until a perception is put from another coroutine."""
    q = PerceptionQueue()
    results: list = []

    async def _producer():
        await asyncio.sleep(0.05)
        q.put(Perception(kind="Awoke", t=0.0, data={}))

    async def _consumer():
        drained = await q.drain()
        results.extend(drained)

    await asyncio.gather(_producer(), _consumer())
    assert len(results) == 1
    assert results[0].kind == "Awoke"


@pytest.mark.asyncio
async def test_drain_returns_empty_on_shutdown() -> None:
    """shutdown() unblocks a waiting drain() with empty list."""
    q = PerceptionQueue()

    async def _shutdown():
        await asyncio.sleep(0.05)
        q.shutdown()

    async def _consumer():
        return await q.drain()

    result = await asyncio.gather(_shutdown(), _consumer())
    drained = result[1]
    assert drained == []
