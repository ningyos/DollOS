"""B3 energy system tests."""
from __future__ import annotations

import asyncio

import pytest

from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from tests._dispatcher_helpers import _make_mind_ctx, _FakeMemSearch
from tests.test_mind_loop import _FakeLLM


def _make_loop(tmp_path, state, queue, ctx, llm_text, energy_enabled=False, cost_per_turn=0.1):
    """Build a MindLoop with energy params for testing."""
    from dollos.mind.mind_loop import MindLoop
    from dollos.tools import MAIN_TOOLS
    return MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=_FakeLLM(llm_text),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "s.json",
        tool_registry={c.__name__: c for c in MAIN_TOOLS},
        energy_enabled=energy_enabled,
        cost_per_turn=cost_per_turn,
    )


# ---------------------------------------------------------------------------
# Task 3: Energy consumption
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_energy_consumed_on_speech_turn(tmp_path):
    """Turn with speech output → energy decreases by cost_per_turn."""
    state = MindState()
    assert state.energy == 1.0

    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    sink = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, sink=sink, state=state)

    # LLM returns speech text (voice_first format: think then speak)
    loop = _make_loop(
        tmp_path, state, queue, ctx,
        llm_text="SEEN: x\nTOOL: none\n</think>\n\nHello there",
        energy_enabled=True,
        cost_per_turn=0.1,
    )
    await loop.iterate()
    assert state.energy == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_energy_not_consumed_on_passive_turn(tmp_path):
    """Turn with no speech/tool output (think-only) → energy unchanged."""
    state = MindState()
    assert state.energy == 1.0

    queue = PerceptionQueue()
    queue.put(Perception(kind="ScheduledMoment", t=2.0, data={"text": "alarm"}))
    sink = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, sink=sink, state=state)

    # LLM returns think-only, no speech, no tool call
    loop = _make_loop(
        tmp_path, state, queue, ctx,
        llm_text="SEEN: x\nTOOL: none\n</think>\n\n",
        energy_enabled=True,
        cost_per_turn=0.1,
    )
    await loop.iterate()
    assert state.energy == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_energy_not_consumed_when_disabled(tmp_path):
    """energy_enabled=False → energy unchanged even on speech turn."""
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    sink = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, sink=sink, state=state)

    loop = _make_loop(
        tmp_path, state, queue, ctx,
        llm_text="SEEN: x\nTOOL: none\n</think>\n\nHello there",
        energy_enabled=False,
        cost_per_turn=0.1,
    )
    await loop.iterate()
    assert state.energy == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_energy_clamp_at_zero(tmp_path):
    """Energy cannot go below 0.0."""
    state = MindState()
    state.energy = 0.03  # less than cost_per_turn

    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    sink = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, sink=sink, state=state)

    loop = _make_loop(
        tmp_path, state, queue, ctx,
        llm_text="SEEN: x\nTOOL: none\n</think>\n\nHello there",
        energy_enabled=True,
        cost_per_turn=0.1,
    )
    await loop.iterate()
    assert state.energy == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Task 4: Energy restoration
# ---------------------------------------------------------------------------

def _mk_trigger(tmp_path, state, **over):
    from dollos.mind.consolidation import ConsolidationTrigger
    from tests._dispatcher_helpers import _FakeMemSearch

    class _FakeRenderer:
        def render(self, template_name: str, **ctx) -> str:
            return f"[stub: {template_name}]"

    defaults = dict(
        state=state, persist_path=tmp_path / "s.json",
        adapter=object(), renderer=_FakeRenderer(), memsearch=_FakeMemSearch(),
        memory_root=tmp_path, transcripts_root=tmp_path / "transcripts",
        tool_output_store=object(), consolidated_dir=tmp_path / "consolidated",
        system_pulse=None,
        idle_threshold_s=300, min_interval_s=3600,
        max_tokens=2048, agent_timeout_s=120, transcript_tail_chars=8000,
        energy_enabled=False, restore_per_tick=0.05,
        energy_idle_threshold_s=600, energy_restore_debounce_s=300,
    )
    defaults.update(over)
    return ConsolidationTrigger(**defaults)


def test_energy_restored_when_user_idle(tmp_path):
    """User has been idle long enough + debounce passed → energy increases."""
    s = MindState(); s.energy = 0.2; s.last_user_at = 0.0; s.last_energy_restore_at = 0.0
    t = _mk_trigger(tmp_path, s, energy_enabled=True, restore_per_tick=0.1,
                    energy_idle_threshold_s=600, energy_restore_debounce_s=300)
    t._maybe_restore_energy(now=10_000.0)  # user idle huge, debounce passed
    assert s.energy == pytest.approx(0.3) and s.last_energy_restore_at == 10_000.0


def test_energy_not_restored_when_user_active(tmp_path):
    """User spoke recently (< idle_threshold) → energy not restored."""
    s = MindState(); s.energy = 0.2; s.last_user_at = 9_900.0
    t = _mk_trigger(tmp_path, s, energy_enabled=True, restore_per_tick=0.1)
    t._maybe_restore_energy(now=10_000.0)  # only 100s idle < 600
    assert s.energy == pytest.approx(0.2)


def test_energy_restore_decoupled_from_consolidation_target(tmp_path):
    """Even with no sealed transcript (target=None path), idle restores energy."""
    s = MindState(); s.energy = 0.2; s.last_user_at = 0.0; s.last_energy_restore_at = 0.0
    t = _mk_trigger(tmp_path, s, energy_enabled=True, restore_per_tick=0.1)
    t._maybe_restore_energy(now=10_000.0)
    assert s.energy == pytest.approx(0.3)  # restored regardless of consolidation target


def test_energy_restore_respects_debounce(tmp_path):
    """Within restore_debounce_s of last restore → no double restore."""
    s = MindState(); s.energy = 0.2; s.last_user_at = 0.0; s.last_energy_restore_at = 9_800.0
    t = _mk_trigger(tmp_path, s, energy_enabled=True, restore_per_tick=0.1,
                    energy_idle_threshold_s=600, energy_restore_debounce_s=300)
    t._maybe_restore_energy(now=10_000.0)  # only 200s since last restore < debounce 300
    assert s.energy == pytest.approx(0.2)


def test_energy_restore_clamp_at_one(tmp_path):
    """Energy cannot exceed 1.0 on restore."""
    s = MindState(); s.energy = 0.97; s.last_user_at = 0.0; s.last_energy_restore_at = 0.0
    t = _mk_trigger(tmp_path, s, energy_enabled=True, restore_per_tick=0.1)
    t._maybe_restore_energy(now=10_000.0)
    assert s.energy == pytest.approx(1.0)


def test_energy_restore_disabled_when_not_enabled(tmp_path):
    """energy_enabled=False → _maybe_restore_energy is a no-op."""
    s = MindState(); s.energy = 0.2; s.last_user_at = 0.0; s.last_energy_restore_at = 0.0
    t = _mk_trigger(tmp_path, s, energy_enabled=False, restore_per_tick=0.1)
    t._maybe_restore_energy(now=10_000.0)
    assert s.energy == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Task 5: Energy injection in [Mind state] + bucket helper
# ---------------------------------------------------------------------------

def test_energy_bucket_line_no_emotion_words():
    from dollos.mind.mind_prompt import energy_bucket_line
    lo = energy_bucket_line(0.3)
    assert "0.3" in lo and "偏低" in lo
    assert "累" not in lo  # no feeling words (autonomy)
    assert "飽滿" in energy_bucket_line(0.9)
    assert "普通" in energy_bucket_line(0.5)


def test_energy_bucket_line_boundaries():
    from dollos.mind.mind_prompt import energy_bucket_line
    # exact boundary: 0.7 → 飽滿
    assert "飽滿" in energy_bucket_line(0.7)
    # just below 0.7 → 普通
    assert "普通" in energy_bucket_line(0.69)
    # exact 0.4 → 普通
    assert "普通" in energy_bucket_line(0.4)
    # just below 0.4 → 偏低
    assert "偏低" in energy_bucket_line(0.39)


def test_render_mind_omits_energy_when_line_none(tmp_path):
    from dollos.mind.mind_prompt import render_mind
    from dollos.mind.mind_state import MindState
    state = MindState()
    prompt = render_mind(state, [], "You are Doll.", energy_line=None)
    assert "精力" not in prompt


def test_render_mind_includes_energy_line(tmp_path):
    from dollos.mind.mind_prompt import render_mind
    from dollos.mind.mind_state import MindState
    state = MindState()
    prompt = render_mind(state, [], "You are Doll.", energy_line="精力: 偏低 (0.3)")
    assert "精力: 偏低 (0.3)" in prompt
