"""AgendaMoment pursuit-turn semantics + energy verification (Task 6, spec
2026-07-07 §4.3). Most of the wiring landed in Task 5 (AgendaObserver,
``_is_agenda`` flag, ``AGENDA_TOOLS`` registry, speech suppression,
kernel observer — tests/test_agenda_observer.py,
tests/test_mind_loop_agenda_registry.py). This file drives a FULL
``AgendaMoment`` turn end-to-end through ``iterate()`` → ``_run_one_turn`` →
``_llm_iterate`` (mirrors tests/test_energy.py's pattern, using a fake LLM
adapter) and verifies the turn's actual pursuit semantics + energy contract:

1. An ``AdvanceGoal`` tool call on an existing ``self_directed`` OpenLoop →
   that loop's ``progress`` gets the new entry, and energy drains
   ``cost_per_turn`` (spec §4.3: "AdvanceGoal(id, progress) append 一條
   進展"; energy drain is the existing generic ``produced`` path,
   ``mind_loop.py`` ~L693 — a tool ran, so ``_turn_had_tool`` is True).
2. A think-only turn (no tool call, no speech text at all) → energy does
   NOT drain — proves throttle (AgendaObserver gate #4, spec §4.1-4), not
   energy, is the rhythm bound for pure-think turns. This is the spec's
   honest §4.1-4/§7 point: "energy 管不住純思考 turn".
3. Speech on an agenda turn never reaches a sink — re-asserted at the FULL
   ``iterate()`` turn level (Task 5's registry tests exercised this via
   ``_run_one_turn`` directly; this test also mixes in a tool call so both
   the pre-tool-call and post-tool-call speech segments are covered).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from dollos.mind.mind_state import MindState, OpenLoop, Perception
from dollos.mind.perception_queue import PerceptionQueue
from tests._mindloop_factory import make_mindloop
from tests.test_mind_loop import _FakeLLM


def _agenda_moment() -> Perception:
    return Perception(kind="AgendaMoment", t=time.time(), data={})


def _seed_self_directed_loop(state: MindState, loop_id: str = "explore_x") -> OpenLoop:
    """A self-directed loop as PursueGoal (Task 2) would have created it —
    real auto-provenance shape, not a placeholder dict."""
    loop = OpenLoop(
        id=loop_id,
        desc="explore something she's curious about",
        opened_at=time.time(),
        self_directed=True,
        trigger="a real memory hit from an earlier conversation",
        provenance={"turn_id": "0", "opened_iter": 0, "memory_sources": []},
        progress=[],
    )
    state.open_loops.append(loop)
    return loop


def _drain_sink(sink: asyncio.Queue) -> list:
    items = []
    while not sink.empty():
        items.append(sink.get_nowait())
    return items


@pytest.mark.asyncio
async def test_advance_goal_turn_appends_progress_and_drains_energy(tmp_path):
    state = MindState()
    loop = _seed_self_directed_loop(state)
    queue = PerceptionQueue(wal=None)
    queue.put(_agenda_moment())

    stream = (
        "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: AdvanceGoal\n</think>\n\n"
        "<tool_call>\n"
        '{"name":"AdvanceGoal","arguments":'
        '{"id":"explore_x","progress":"realized a connection"}}\n'
        "</tool_call>"
    )
    ml = make_mindloop(
        memory_root=tmp_path,
        state=state,
        queue=queue,
        llm=_FakeLLM(stream),
        energy_enabled=True,
        cost_per_turn=0.05,
    )

    assert state.energy == 1.0
    await ml.iterate()

    assert loop.progress == ["realized a connection"], (
        f"AdvanceGoal must append to the matching self_directed loop's "
        f"progress; got {loop.progress!r}"
    )
    assert state.energy == pytest.approx(0.95), (
        "a tool ran this turn (_turn_had_tool) → produced=True → energy "
        "must drain cost_per_turn via the existing generic path"
    )


@pytest.mark.asyncio
async def test_think_only_agenda_turn_does_not_drain_energy(tmp_path):
    """No tool call, no speech text at all → produced is False → energy
    unchanged. This is the spec's honest §4.1-4/§7 point: energy cannot
    bound a pure-think agenda turn (a turn that only thinks and calls no
    tool costs nothing) — throttle (AgendaObserver gate #4) is the actual
    rhythm bound, asserted here by contrast with the draining test above."""
    state = MindState()
    _seed_self_directed_loop(state)
    queue = PerceptionQueue(wal=None)
    queue.put(_agenda_moment())

    stream = "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: none\n</think>\n\n"
    ml = make_mindloop(
        memory_root=tmp_path,
        state=state,
        queue=queue,
        llm=_FakeLLM(stream),
        energy_enabled=True,
        cost_per_turn=0.05,
    )

    assert state.energy == 1.0
    await ml.iterate()

    assert state.energy == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_agenda_turn_speech_never_reaches_sink_at_turn_level(tmp_path):
    """Re-assert Task 5's R1-M1 suppression (test_mind_loop_agenda_registry.py
    exercised it via ``_run_one_turn`` directly) via the FULL ``iterate()``
    turn — queue drain + ``_run_one_turn`` + ``_llm_iterate`` — with a fake
    LLM that emits speech BOTH before and after the tool call, matching the
    voice_first interleaved wire format used elsewhere
    (tests/test_mind_loop.py::test_iterate_streams_speech_to_sink_and_dispatches_tool)."""
    state = MindState()
    _seed_self_directed_loop(state)
    queue = PerceptionQueue(wal=None)
    queue.put(_agenda_moment())
    sink: asyncio.Queue = asyncio.Queue()

    stream = (
        "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: AdvanceGoal\n</think>\n\n"
        "thinking out loud first"
        "<tool_call>\n"
        '{"name":"AdvanceGoal","arguments":'
        '{"id":"explore_x","progress":"a step"}}\n'
        "</tool_call>"
        " and after"
    )
    ml = make_mindloop(
        memory_root=tmp_path,
        state=state,
        queue=queue,
        sink=sink,
        llm=_FakeLLM(stream),
    )

    await ml.iterate()

    items = _drain_sink(sink)
    speech_items = [i for i in items if i is not None]
    assert speech_items == [], (
        f"AgendaMoment turn must not push speech to the sink even when tool "
        f"call + speech are interleaved, got: {speech_items}"
    )
