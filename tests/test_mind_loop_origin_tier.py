"""Per-turn origin_tier axis (P1e Task 1, spec S1; C1 fix from the
whole-branch review, 2026-07-05).

`MindCtx.origin_tier` is computed once per bucket at drain time from that
bucket's perceptions: a ChannelMessage from the owner AND in a DM
(``is_dm``) → "external_dm"; anyone else — including the owner posting in a
PUBLIC channel — → "external_public"; no ChannelMessage → "internal" (P1a
single-origin bucket, so this is a straight scan, no cross-bucket bleed).

Also covers the S1 side-effect: ChannelMessage now counts toward
`_EXTERNAL_KINDS`, so a Discord-originated turn sets `ctx.external_ctx = True`
(previously only ToolResultArrived/MonitorFired/MonitorEnded did) — this is
what down-weights PinSelf writes made on Discord turns via the existing
self_history provenance path.
"""
from __future__ import annotations

import time

import pytest

from dollos.mind.mind_state import Perception
from tests._mindloop_factory import make_mindloop


def _channel_msg(
    content: str,
    *,
    author_is_owner: bool,
    is_dm: bool = False,
    channel_id: str = "disc:g1:c1",
) -> Perception:
    return Perception(
        kind="ChannelMessage",
        t=time.time(),
        data={
            "content": content,
            "channel_id": channel_id,
            "author_is_owner": author_is_owner,
            "is_dm": is_dm,
        },
    )


def _user_perception(text: str) -> Perception:
    return Perception(kind="UserSpoke", t=time.time(), data={"text": text})


@pytest.mark.asyncio
async def test_origin_tier_owner_dm(tmp_path):
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn(
        [_channel_msg("hi", author_is_owner=True, is_dm=True)]
    )
    assert ml._ctx.origin_tier == "external_dm"
    assert ml._ctx.external_ctx is True  # S1: ChannelMessage now external


@pytest.mark.asyncio
async def test_origin_tier_owner_in_public_channel_is_external_public(tmp_path):
    """Whole-branch review C1: the owner posting in a PUBLIC channel
    (author_is_owner=True, is_dm=False) must NOT get "external_dm" — the
    reply there is public, so full private retrieval must not be granted."""
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn(
        [_channel_msg("hi", author_is_owner=True, is_dm=False)]
    )
    assert ml._ctx.origin_tier == "external_public"


@pytest.mark.asyncio
async def test_origin_tier_owner_missing_is_dm_fails_closed(tmp_path):
    """A ChannelMessage that omits ``is_dm`` entirely (falsy via .get()) must
    fail CLOSED to external_public, not open to external_dm."""
    ml = make_mindloop(memory_root=tmp_path)
    p = Perception(
        kind="ChannelMessage",
        t=time.time(),
        data={"content": "hi", "channel_id": "disc:g1:c1", "author_is_owner": True},
    )
    await ml._run_one_turn([p])
    assert ml._ctx.origin_tier == "external_public"


@pytest.mark.asyncio
async def test_origin_tier_stranger(tmp_path):
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn([_channel_msg("hi", author_is_owner=False)])
    assert ml._ctx.origin_tier == "external_public"
    assert ml._ctx.external_ctx is True  # S1: ChannelMessage now external


@pytest.mark.asyncio
async def test_origin_tier_internal_user_spoke(tmp_path):
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn([_user_perception("hi")])
    assert ml._ctx.origin_tier == "internal"
    assert ml._ctx.external_ctx is False


@pytest.mark.asyncio
async def test_origin_tier_no_bleed_across_buckets(tmp_path):
    """Per-bucket recompute (P1a single-origin turn): running an external
    bucket then an internal bucket must not leave the external tier stuck."""
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn([_channel_msg("hi", author_is_owner=False)])
    assert ml._ctx.origin_tier == "external_public"

    await ml._run_one_turn([_user_perception("hi")])
    assert ml._ctx.origin_tier == "internal"  # recomputed, no bleed
    assert ml._ctx.external_ctx is False
