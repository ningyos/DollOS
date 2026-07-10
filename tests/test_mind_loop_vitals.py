"""Tests wiring VitalsRecorder into MindLoop's drain site (代謝 vital spec
2026-07-10 §2.2, Task 3).

Pins the cross-task correctness requirement: the VitalsRecord emission MUST
sit INSIDE the same ``if self._energy_enabled and produced and consumes:``
drain gate Task 2 stashes `_turn_energy_cost`/`_turn_cost_mode`/
`_turn_tokens_total` in. On a gate-skipped turn (e.g. ``external_public``)
those stash fields retain the PREVIOUS draining turn's values — so emitting
unconditionally at end-of-turn would silently write a wrong vitals row for a
turn that never actually drained. Mirrors tests/test_energy_origin.py's
fixture for the gate-skip scenario and
tests/test_mind_loop_turn_latency.py's ``_CapturingRecorder`` pattern.
"""
from __future__ import annotations

import time

import pytest

from dollos.mind.mind_state import Perception
from tests._mindloop_factory import make_mindloop
from tests.test_mind_loop import _ScriptedLLM, _speech_pass


class _CapturingRecorder:
    def __init__(self):
        self.records = []

    async def record(self, rec):
        self.records.append(rec)


def _user_perception(text: str) -> Perception:
    return Perception(kind="UserSpoke", t=time.time(), data={"text": text})


def _channel_msg(content: str, *, author_is_owner: bool, t: float) -> Perception:
    return Perception(
        kind="ChannelMessage",
        t=t,
        data={
            "content": content,
            "channel_id": "disc:g1:c1",
            "author_is_owner": author_is_owner,
            "is_dm": False,
        },
    )


@pytest.mark.asyncio
async def test_draining_turn_emits_vitals_row(tmp_path):
    """An internal (local chat) turn drains energy — the gate runs, so a
    VitalsRecord must be emitted with the same values that landed in the
    stash fields."""
    rec = _CapturingRecorder()
    llm = _ScriptedLLM([_speech_pass("你好")])
    ml = make_mindloop(
        memory_root=tmp_path, llm=llm,
        energy_enabled=True, cost_per_turn=0.1,
        vitals_recorder=rec,
    )

    await ml._run_one_turn([_user_perception("hi")])

    assert len(rec.records) == 1
    r = rec.records[0]
    assert r.energy_cost == pytest.approx(0.1)
    assert r.energy_after == pytest.approx(0.9)
    assert r.cost_mode == "flat_legacy"  # no token usage callback wired here


@pytest.mark.asyncio
async def test_gate_skipped_turn_emits_no_vitals_row(tmp_path):
    """A stranger's Discord turn (origin_tier=="external_public") is exempt
    from the energy-drain gate (P1e Task 5, I4) — it must ALSO emit no
    vitals row, EVEN when a prior turn this loop instance already drained
    (leaving the stash fields non-default). If VitalsRecord were emitted
    unconditionally at end-of-turn instead of inside the gate, this turn
    would wrongly emit a SECOND row carrying over the FIRST (draining)
    turn's stale `_turn_energy_cost`/`_turn_cost_mode`/`_turn_tokens_total`
    stash — this test's sequencing (drain, then skip) is what would catch
    that regression; a skip-turn-only test would pass even with the bug,
    since the stash's own __init__ defaults look innocuous."""
    rec = _CapturingRecorder()
    llm = _ScriptedLLM([_speech_pass("hi"), _speech_pass("hi again")])
    ml = make_mindloop(
        memory_root=tmp_path, llm=llm,
        energy_enabled=True, cost_per_turn=0.1,
        vitals_recorder=rec,
    )

    # First: a real draining turn — stashes non-default energy_cost/cost_mode.
    await ml._run_one_turn([_user_perception("hi")])
    assert len(rec.records) == 1

    # Second: a gate-skipped stranger turn — must add NO new row, despite
    # the stash still holding the first turn's values.
    await ml._run_one_turn(
        [_channel_msg("hi", author_is_owner=False, t=1.0)]
    )

    assert len(rec.records) == 1, (
        "gate-skipped turn must not emit a vitals row from the stale stash"
    )


@pytest.mark.asyncio
async def test_no_vitals_recorder_is_a_noop(tmp_path):
    """vitals_recorder=None (default) must not raise or break the turn."""
    llm = _ScriptedLLM([_speech_pass("你好")])
    ml = make_mindloop(memory_root=tmp_path, llm=llm, energy_enabled=True)

    await ml._run_one_turn([_user_perception("hi")])  # must not raise
