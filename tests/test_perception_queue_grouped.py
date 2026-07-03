"""drain_grouped — per-origin turn segmentation (spec §3.1 R2-C1)."""
import time

import pytest

from dollos.mind.mind_state import Perception
from dollos.mind.perception_queue import PerceptionQueue


def _p(kind, **data):
    return Perception(kind=kind, t=time.time(), data=data)


@pytest.mark.asyncio
async def test_groups_by_channel_preserving_order():
    q = PerceptionQueue()
    q.put(_p("ChannelMessage", channel_id="A", content="a1"))
    q.put(_p("ChannelMessage", channel_id="B", content="b1"))
    q.put(_p("ChannelMessage", channel_id="A", content="a2"))
    buckets = await q.drain_grouped()
    # two buckets; A keeps [a1,a2] order; B is [b1]
    got = {b[0].data["channel_id"]: [p.data["content"] for p in b] for b in buckets}
    assert got == {"A": ["a1", "a2"], "B": ["b1"]}


@pytest.mark.asyncio
async def test_originless_share_one_internal_bucket():
    q = PerceptionQueue()
    q.put(_p("UserSpoke", text="hi"))          # no channel_id
    q.put(_p("ReflectionMoment"))              # no channel_id
    buckets = await q.drain_grouped()
    assert len(buckets) == 1 and len(buckets[0]) == 2


@pytest.mark.asyncio
async def test_shutdown_returns_empty():
    q = PerceptionQueue()
    q.shutdown()
    assert await q.drain_grouped() == []
