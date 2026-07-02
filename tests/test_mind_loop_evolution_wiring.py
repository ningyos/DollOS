"""MindLoop wires SelfRevision into the reflection registry/grammar + the
evolution render-path hooks (spec §3.4/§5).

The `test_wiring_*` tests mutation-proof the dead zones the whole-branch review
(F6) flagged: deleting the drain-time latch/flag reset, the process_tripwire
call, or the surface_or_expire call must break the suite.
"""
import asyncio

import pytest

from dollos.mind import evolution as evo
from dollos.mind import self_history
from dollos.mind.mind_state import Perception
from dollos.mind.perception_queue import PerceptionQueue
from tests._mindloop_factory import make_mindloop


def test_reflection_registry_includes_self_revision_when_enabled(tmp_path):
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True)
    ml._is_reflection = True
    ml._state.safe_mode = False
    assert "SelfRevision" in ml._active_tool_registry()


def test_reflection_registry_excludes_self_revision_when_disabled(tmp_path):
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=False)
    ml._is_reflection = True
    ml._state.safe_mode = False
    assert "SelfRevision" not in ml._active_tool_registry()


def test_safe_mode_excludes_self_revision(tmp_path):
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True)
    ml._is_reflection = True
    ml._state.safe_mode = True
    assert "SelfRevision" not in ml._active_tool_registry()


def test_self_revision_in_refeed_allowlist():
    from dollos.mind.mind_loop import IN_TURN_REFEED_TOOLS
    assert "SelfRevision" in IN_TURN_REFEED_TOOLS


# --- F6 wiring: the render-path dead zones ---


class _CapturingLLM:
    """Minimal terminal-stream LLM that records the rendered prompt (``user``)."""

    def __init__(self):
        self.last_user = None

    async def stream_completion(self, system, user, prefill, max_tokens=1024,
                                grammar=None, purpose="cascade"):
        self.last_user = user

        class _Chunk:
            text = "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: none\n</think>\n\nhi"
            done = True

        yield _Chunk()


@pytest.mark.asyncio
async def test_wiring_latch_and_surfaced_flag_reset_at_drain(tmp_path):
    """F6(a): the per-turn latch AND the surfaced-this-turn flag both reset at
    drain. Drive two iterates; a latch/flag set after turn 1 must be cleared by
    turn 2's drain (delete the reset → this fails)."""
    queue = PerceptionQueue(wal=None)
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True,
                       sink=asyncio.Queue(), queue=queue)
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    await ml.iterate()
    # Simulate a SelfRevision call having latched + a surfacing having armed.
    ml._ctx.evolution_latched = True
    ml._ctx.evolution_candidate_surfaced = True
    queue.put(Perception(kind="UserSpoke", t=2.0, data={"text": "again"}))
    await ml.iterate()
    assert ml._ctx.evolution_latched is False
    assert ml._ctx.evolution_candidate_surfaced is False


@pytest.mark.asyncio
async def test_wiring_iterate_calls_process_tripwire(tmp_path):
    """F6(b): iterate() runs the tamper tripwire when evolution is enabled.
    Seed a crash_repair state (sanctioned in the log, file == old_text) → one
    driven iterate must heal the file + log evo_repair (delete the call → fails)."""
    hist = tmp_path / "self_history.jsonl"
    self_history.log_event(hist, kind="evo_adopt", text="新版" + "字" * 88,
                           old_text="舊版" + "字" * 88, drift_score=0.1)
    cs = tmp_path / "current_self.md"
    cs.write_text("舊版" + "字" * 88, encoding="utf-8")  # == old_text: crash window
    queue = PerceptionQueue(wal=None)
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True,
                       sink=asyncio.Queue(), queue=queue)
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    await ml.iterate()
    assert cs.read_text(encoding="utf-8") == "新版" + "字" * 88   # healed
    assert [e["kind"] for e in self_history.read_events(hist)][-1] == "evo_repair"


@pytest.mark.asyncio
async def test_wiring_iterate_does_not_tripwire_when_disabled(tmp_path):
    """F6(b) boundary: with evolution disabled, iterate() must NOT run the
    tripwire — a divergent file is left alone, no evo_repair logged."""
    hist = tmp_path / "self_history.jsonl"
    self_history.log_event(hist, kind="evo_adopt", text="新版" + "字" * 88,
                           old_text="舊版" + "字" * 88, drift_score=0.1)
    cs = tmp_path / "current_self.md"
    cs.write_text("舊版" + "字" * 88, encoding="utf-8")
    queue = PerceptionQueue(wal=None)
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=False,
                       sink=asyncio.Queue(), queue=queue)
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    await ml.iterate()
    assert cs.read_text(encoding="utf-8") == "舊版" + "字" * 88   # untouched
    assert "evo_repair" not in [e["kind"] for e in self_history.read_events(hist)]


@pytest.mark.asyncio
async def test_wiring_iterate_calls_surface_or_expire_on_reflection(tmp_path):
    """F6(c): iterate() surfaces an awaiting_doll slot on reflection turns — the
    surfaced_count increments, the ctx flag arms, and the [人格演化候選] block
    reaches the rendered prompt (delete surface_or_expire → all three fail)."""
    sp = tmp_path / "self_evolution" / "pending.json"
    evo.save_slot(sp, evo.make_keeper_slot(
        candidate="我現在監控數字時會主動來勁。" + "細節" * 30,
        rationale="活很久的 pin", hwm_before=None, created_ts=0.0))
    llm = _CapturingLLM()
    queue = PerceptionQueue(wal=None)
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True,
                       sink=asyncio.Queue(), queue=queue, llm=llm)
    queue.put(Perception(kind="ReflectionMoment", t=1.0, data={}))
    await ml.iterate()
    assert evo.load_slot(sp).surfaced_count == 1              # surface ran
    assert ml._ctx.evolution_candidate_surfaced is True        # gate armed
    assert "[人格演化候選]" in llm.last_user                    # block rendered


@pytest.mark.asyncio
async def test_wiring_no_surface_on_non_reflection_turn(tmp_path):
    """F6(c) boundary: a non-reflection turn never surfaces (surfaced_count
    stays 0, flag stays False) even with an awaiting_doll slot present."""
    sp = tmp_path / "self_evolution" / "pending.json"
    evo.save_slot(sp, evo.make_keeper_slot(
        candidate="我現在監控數字時會主動來勁。" + "細節" * 30,
        rationale="R", hwm_before=None, created_ts=0.0))
    queue = PerceptionQueue(wal=None)
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True,
                       sink=asyncio.Queue(), queue=queue)
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    await ml.iterate()
    assert evo.load_slot(sp).surfaced_count == 0
    assert ml._ctx.evolution_candidate_surfaced is False
