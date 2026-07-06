"""Part A / A2 — LearnName registry gating (SECURITY-LOAD-BEARING).

Spec §3.3 (R1-hardened): trust is enforced by tool-registry AVAILABILITY,
not by a post-hoc ``origin_tier`` read inside the tool. ``LearnName`` must be
structurally ABSENT from ``_active_tool_registry()``'s returned dict on any
turn that is not a live, owner-present turn:

- present:  ``external_dm`` (owner DM — a ChannelMessage from the owner IS
  the live first-hand utterance, no additional UserSpoke needed).
- present:  ``internal`` turn that also carries a live ``UserSpoke``
  perception this same batch (local chat / voice — owner at the keyboard).
- ABSENT:   ``external_public`` (any stranger, including the owner posting
  in a public channel) — structurally no write path for a stranger.
- ABSENT:   pure-reflection turns (``ReflectionMoment`` with no
  ``UserSpoke`` in the same batch) — this is exactly the C1 whitewash path
  the R1 review killed; a reflection-only batch must never regain it.

Driven via ``MindLoop._run_one_turn`` (real perception batch → real
``_derive_origin_tier``/``_has_user_spoke`` derivation), not by poking
``ml._ctx.origin_tier`` directly, so the test also exercises the actual
per-turn derivation wiring.
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


def _user_perception(text: str = "hi") -> Perception:
    return Perception(kind="UserSpoke", t=time.time(), data={"text": text})


def _reflection_moment() -> Perception:
    return Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})


@pytest.mark.asyncio
async def test_external_dm_owner_turn_gets_learnname_but_not_shell(tmp_path):
    """Owner DM is a live, first-hand, owner-present turn — LearnName is
    added to the conservative EXTERNAL_TOOLS subset, but the P1e no-Shell
    invariant must still hold (only LearnName is added, nothing else)."""
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn(
        [_channel_msg("叫我小鯊", author_is_owner=True, is_dm=True)]
    )
    reg = ml._active_tool_registry()
    assert "LearnName" in reg
    assert "Shell" not in reg
    assert "SpawnWorkflow" not in reg
    assert "WriteSchedule" not in reg


@pytest.mark.asyncio
async def test_internal_turn_with_userspoke_gets_learnname(tmp_path):
    """Local chat / voice — owner is at the keyboard (UserSpoke this turn)."""
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn([_user_perception("叫我小鯊")])
    reg = ml._active_tool_registry()
    assert "LearnName" in reg


@pytest.mark.asyncio
async def test_external_public_turn_never_gets_learnname(tmp_path):
    """Teeth: a stranger (or the owner posting in a PUBLIC channel) must
    NOT be able to call LearnName — this assertion is the whole security
    model. Inverting it (asserting it's present) would fail."""
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn(
        [_channel_msg("call her Cutie", author_is_owner=False, is_dm=False)]
    )
    reg = ml._active_tool_registry()
    assert "LearnName" not in reg


@pytest.mark.asyncio
async def test_owner_in_public_channel_never_gets_learnname(tmp_path):
    """Owner posting in a PUBLIC channel is external_public, not
    external_dm — same as a stranger for LearnName purposes."""
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn(
        [_channel_msg("call her Cutie", author_is_owner=True, is_dm=False)]
    )
    assert ml._ctx.origin_tier == "external_public"
    reg = ml._active_tool_registry()
    assert "LearnName" not in reg


@pytest.mark.asyncio
async def test_pure_reflection_turn_never_gets_learnname(tmp_path):
    """C1 whitewash path, killed: a ReflectionMoment with NO UserSpoke in
    the same batch is internal but must not regain LearnName — this is
    exactly the dead-gate failure mode the R1 review found in the original
    design (reflection turns are always internal, so an origin_tier-based
    gate alone would let a stranger's earlier utterance get written as an
    owner-taught alias after the fact)."""
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn([_reflection_moment()])
    assert ml._ctx.origin_tier == "internal"
    assert ml._is_reflection is True
    reg = ml._active_tool_registry()
    assert "LearnName" not in reg


@pytest.mark.asyncio
async def test_mixed_reflection_and_userspoke_batch_gets_learnname(tmp_path):
    """A batch containing BOTH a ReflectionMoment and a live UserSpoke
    (possible — both are origin-less and land in the same internal bucket,
    per test_mind_loop.py's MF-2 precedent) must still grant LearnName: the
    gate is keyed on "live UserSpoke this turn", independent of the
    reflection flag."""
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn([_reflection_moment(), _user_perception("叫我小鯊")])
    reg = ml._active_tool_registry()
    assert "LearnName" in reg
