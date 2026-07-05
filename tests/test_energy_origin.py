"""Energy origin-awareness (P1e Task 5, spec I4).

A stranger's Discord message (origin_tier="external_public") must not drain
Doll's energy budget — that budget gates sleep-consolidation, and letting
random Discord traffic burn it would mean a stranger can suppress her
consolidation cycle for free. An owner DM (origin_tier="external_dm") is
upgraded to UserSpoke-equivalent: it consumes energy same as local chat AND
advances `last_user_at` (so "owner present" gating — e.g. energy restore
pause — reacts to owner DMs the same way it reacts to local chat).

Uses `_run_one_turn` directly (mirrors tests/test_mind_loop_origin_tier.py)
since both the energy-consumption gate and the last_user_at advance live
inside that method's body — no need to round-trip through the perception
queue.
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
    t: float,
    is_dm: bool = False,
    channel_id: str = "disc:g1:c1",
) -> Perception:
    return Perception(
        kind="ChannelMessage",
        t=t,
        data={
            "content": content,
            "channel_id": channel_id,
            "author_is_owner": author_is_owner,
            "is_dm": is_dm,
        },
    )


def _user_perception(text: str, *, t: float) -> Perception:
    return Perception(kind="UserSpoke", t=t, data={"text": text})


# ---------------------------------------------------------------------------
# Energy consumption
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_external_public_turn_does_not_drain_energy(tmp_path):
    """A stranger's Discord turn produces speech (default fake LLM) but must
    not cost energy — stranger social shouldn't drain her energy-with-owner
    budget."""
    ml = make_mindloop(memory_root=tmp_path, energy_enabled=True, cost_per_turn=0.1)
    assert ml._state.energy == pytest.approx(1.0)

    await ml._run_one_turn([_channel_msg("hi", author_is_owner=False, t=1.0)])

    assert ml._state.energy == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_internal_turn_drains_energy_control(tmp_path):
    """Control: an internal (non-ChannelMessage) turn still drains energy
    exactly as before — this task must not touch the internal/local path."""
    ml = make_mindloop(memory_root=tmp_path, energy_enabled=True, cost_per_turn=0.1)
    assert ml._state.energy == pytest.approx(1.0)

    await ml._run_one_turn([_user_perception("hi", t=1.0)])

    assert ml._state.energy == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_external_dm_owner_turn_drains_energy_upgraded(tmp_path):
    """An owner DM (external_dm) is upgraded to UserSpoke-equivalent: it
    still consumes energy like local chat."""
    ml = make_mindloop(memory_root=tmp_path, energy_enabled=True, cost_per_turn=0.1)
    assert ml._state.energy == pytest.approx(1.0)

    await ml._run_one_turn(
        [_channel_msg("hi", author_is_owner=True, is_dm=True, t=1.0)]
    )

    assert ml._state.energy == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# last_user_at owner-DM upgrade
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_external_dm_owner_turn_advances_last_user_at(tmp_path):
    """Owner DM upgrade (S2): advances last_user_at + user_turn_count just
    like UserSpoke, so owner-present gating (e.g. energy restore pause)
    reacts to owner DMs too."""
    ml = make_mindloop(memory_root=tmp_path, energy_enabled=True, cost_per_turn=0.1)
    assert ml._state.last_user_at == pytest.approx(0.0)
    assert ml._state.user_turn_count == 0

    t = 12345.0
    await ml._run_one_turn(
        [_channel_msg("hi", author_is_owner=True, is_dm=True, t=t)]
    )

    assert ml._state.last_user_at == pytest.approx(t)
    assert ml._state.user_turn_count == 1


@pytest.mark.asyncio
async def test_external_public_turn_does_not_advance_last_user_at(tmp_path):
    """A stranger's Discord turn must NOT count as "owner present" — it must
    not advance last_user_at or user_turn_count."""
    ml = make_mindloop(memory_root=tmp_path, energy_enabled=True, cost_per_turn=0.1)

    await ml._run_one_turn([_channel_msg("hi", author_is_owner=False, t=99999.0)])

    assert ml._state.last_user_at == pytest.approx(0.0)
    assert ml._state.user_turn_count == 0


@pytest.mark.asyncio
async def test_internal_user_spoke_turn_advances_last_user_at_control(tmp_path):
    """Control: plain UserSpoke (local chat) still advances last_user_at —
    unchanged by this task."""
    ml = make_mindloop(memory_root=tmp_path, energy_enabled=True, cost_per_turn=0.1)

    t = 54321.0
    await ml._run_one_turn([_user_perception("hi", t=t)])

    assert ml._state.last_user_at == pytest.approx(t)
    assert ml._state.user_turn_count == 1
