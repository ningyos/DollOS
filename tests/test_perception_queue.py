import asyncio
import time
from pathlib import Path

import pytest

from dollos.mind.perception_queue import PerceptionQueue
from dollos.mind.mind_state import Perception
from dollos.wal.perception_log import PerceptionWAL


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


@pytest.mark.asyncio
async def test_perception_queue_writes_wal_on_put(tmp_path: Path):
    wal = PerceptionWAL(tmp_path / "wal.jsonl")
    queue = PerceptionQueue(wal=wal)
    queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"}))
    queue.put(Perception(kind="ScheduledMoment", t=time.time(), data={}))

    pending = list(wal.iter_pending())
    assert len(pending) == 2
    assert pending[0][1].kind == "UserSpoke"


@pytest.mark.asyncio
async def test_perception_queue_no_wal_still_works():
    """When wal=None, queue still works (backwards compat)."""
    queue = PerceptionQueue()  # no wal arg
    queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"}))
    drained = await queue.drain(timeout_s=0.5)
    assert len(drained) == 1


@pytest.mark.asyncio
async def test_perception_queue_attaches_seq_to_drained(tmp_path: Path):
    wal = PerceptionWAL(tmp_path / "wal.jsonl")
    queue = PerceptionQueue(wal=wal)
    queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"}))
    queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "bye"}))

    drained = await queue.drain(timeout_s=0.5)
    seqs = [d.seq for d in drained]
    assert seqs[0] is not None
    assert seqs[1] is not None
    assert seqs[1] == seqs[0] + 1


@pytest.mark.asyncio
async def test_perception_queue_replay_does_not_double_log(tmp_path: Path):
    """A perception arriving with seq already set (from WAL replay) doesn't re-append."""
    wal = PerceptionWAL(tmp_path / "wal.jsonl")
    queue = PerceptionQueue(wal=wal)

    # Simulate replay: perception has seq=42 already
    p = Perception(kind="UserSpoke", t=time.time(), data={"text": "replay"}, seq=42)
    queue.put(p)

    # WAL should be empty (no new append from this put)
    pending = list(wal.iter_pending())
    assert pending == []

    drained = await queue.drain(timeout_s=0.5)
    assert drained[0].seq == 42
