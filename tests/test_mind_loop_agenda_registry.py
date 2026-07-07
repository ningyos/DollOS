"""Self-directed agenda — pure-agenda registry enforcement (SECURITY-LOAD-BEARING,
spec 2026-07-07 §5.2, R1-C1).

Mirrors tests/test_mind_loop_learnname_gate.py's pattern: trust is enforced by
tool-registry AVAILABILITY. A pure ``AgendaMoment`` turn (no ``UserSpoke`` in
the same batch) must get EXACTLY the ``AGENDA_TOOLS`` subset — no Shell,
SpawnWorkflow, SelfRevision, NoteMemory, or PursueGoal. The critical negative
case (the whole reason this is load-bearing): an ``AgendaMoment`` co-batched
with a live ``UserSpoke`` (both origin-less internal perceptions — the same
MF-2 shape as test_mind_loop.py's reflection+UserSpoke case) must NOT be
treated as a pure-agenda turn — the real user's request must keep the FULL
registry (Shell/SpawnWorkflow present), never silently downgraded to the
agenda subset.

Also covers R1-M1 streamed-text suppression: an AgendaMoment turn must not
push any speech to the sink.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from dollos.mind.mind_state import Perception
from dollos.tools import AGENDA_TOOLS
from tests._mindloop_factory import make_mindloop


def _agenda_moment() -> Perception:
    return Perception(kind="AgendaMoment", t=time.time(), data={})


def _user_perception(text: str = "hi") -> Perception:
    return Perception(kind="UserSpoke", t=time.time(), data={"text": text})


def _monitor_fired_perception() -> Perception:
    return Perception(
        kind="MonitorFired",
        t=time.time(),
        data={"monitor_id": "m1", "line": "some output line"},
    )


@pytest.mark.asyncio
async def test_pure_agenda_turn_gets_exactly_agenda_tools(tmp_path):
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn([_agenda_moment()])

    assert ml._is_agenda is True
    reg = ml._active_tool_registry()
    assert set(reg.keys()) == set(AGENDA_TOOLS)
    for forbidden in ("Shell", "SpawnWorkflow", "SelfRevision", "NoteMemory", "PursueGoal"):
        assert forbidden not in reg


@pytest.mark.asyncio
async def test_agenda_and_userspoke_cobatch_keeps_full_registry(tmp_path):
    """R1-C1 whitewash guard: a real user request co-batched with an
    AgendaMoment must NEVER be silently restricted to AGENDA_TOOLS."""
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn([_agenda_moment(), _user_perception("hi")])

    assert ml._is_agenda is False, (
        "_is_agenda must be False when a live UserSpoke is co-batched — "
        "this is the C1 safety, not incidental"
    )
    reg = ml._active_tool_registry()
    assert "Shell" in reg
    assert "SpawnWorkflow" in reg
    assert set(AGENDA_TOOLS).issubset(reg.keys())  # full registry is a superset


@pytest.mark.asyncio
async def test_userspoke_only_turn_is_not_agenda(tmp_path):
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn([_user_perception("hi")])
    assert ml._is_agenda is False
    reg = ml._active_tool_registry()
    assert "Shell" in reg


def _drain_sink(sink: asyncio.Queue) -> list:
    items = []
    while not sink.empty():
        items.append(sink.get_nowait())
    return items


@pytest.mark.asyncio
async def test_agenda_moment_turn_does_not_emit_speech_to_sink(tmp_path):
    """R1-M1: an AgendaMoment turn is her thinking internally, not talking to
    the local sink — streamed text must not reach it, even though the fake
    LLM's canned response contains speech ("hi") with no tool call. The
    end-of-turn ``None`` pump sentinel (always fired, every turn, unrelated
    to speech) is expected and excluded from this assertion."""
    sink: asyncio.Queue = asyncio.Queue()
    ml = make_mindloop(memory_root=tmp_path, sink=sink)

    await ml._run_one_turn([_agenda_moment()])

    items = _drain_sink(sink)
    speech_items = [i for i in items if i is not None]
    assert speech_items == [], f"AgendaMoment turn must not push speech, got: {speech_items}"


@pytest.mark.asyncio
async def test_reactive_turn_still_emits_speech_to_sink(tmp_path):
    """Contrast: a normal UserSpoke turn with the same fake LLM DOES reach
    the sink — proves the suppression in the previous test is agenda-specific,
    not a global regression."""
    sink: asyncio.Queue = asyncio.Queue()
    ml = make_mindloop(memory_root=tmp_path, sink=sink)

    await ml._run_one_turn([_user_perception("hi")])

    items = _drain_sink(sink)
    speech_items = [i for i in items if i is not None]
    assert speech_items != []


@pytest.mark.asyncio
async def test_agenda_cobatched_with_other_internal_perception_keeps_full_registry_and_speech(tmp_path):
    """Whole-branch review Finding 1 (operational-safety): ``drain_grouped``
    (perception_queue.py:85) batches ALL origin-less internal perceptions
    into ONE bucket — not just AgendaMoment+UserSpoke. An AgendaMoment
    co-batched with ANY other origin-less perception (MonitorFired here,
    stands in for ToolResultArrived/ScheduledMoment/Awoke/BridgeDown/etc.)
    must NOT be treated as a pure-agenda turn either — otherwise a monitor
    alert lands in the same batch as a stray AgendaMoment and gets silently
    downgraded to AGENDA_TOOLS with its speech suppressed."""
    sink: asyncio.Queue = asyncio.Queue()
    ml = make_mindloop(memory_root=tmp_path, sink=sink)

    await ml._run_one_turn([_agenda_moment(), _monitor_fired_perception()])

    assert ml._is_agenda is False, (
        "_is_agenda must be False when co-batched with ANY other origin-less "
        "internal perception, not just UserSpoke"
    )
    reg = ml._active_tool_registry()
    assert "Shell" in reg
    assert set(AGENDA_TOOLS).issubset(reg.keys())  # full registry is a superset

    items = _drain_sink(sink)
    speech_items = [i for i in items if i is not None]
    assert speech_items != [], "the co-batched turn's speech must not be swallowed"
