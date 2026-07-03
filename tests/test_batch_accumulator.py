"""BatchAccumulator — same-channel coalescing window (spec §3.1 I1)."""
import asyncio

import pytest

from dollos.ipc.batch_accumulator import BatchAccumulator


@pytest.mark.asyncio
async def test_coalesces_same_channel_within_window():
    flushed = []
    acc = BatchAccumulator(enqueue=lambda items: flushed.append(items), window_s=0.05)
    await acc.add("A", {"n": 1})
    await acc.add("A", {"n": 2})       # within window → same batch
    await asyncio.sleep(0.08)
    assert flushed == [[{"n": 1}, {"n": 2}]]


@pytest.mark.asyncio
async def test_separate_channels_separate_batches():
    flushed = []
    acc = BatchAccumulator(enqueue=lambda items: flushed.append(items), window_s=0.05)
    await acc.add("A", {"n": 1})
    await acc.add("B", {"n": 9})
    await asyncio.sleep(0.08)
    assert [{"n": 1}] in flushed and [{"n": 9}] in flushed and len(flushed) == 2


@pytest.mark.asyncio
async def test_flush_all_drains_immediately():
    flushed = []
    acc = BatchAccumulator(enqueue=lambda items: flushed.append(items), window_s=10.0)
    await acc.add("A", {"n": 1})
    await acc.flush_all()              # e.g. shutdown
    assert flushed == [[{"n": 1}]]
