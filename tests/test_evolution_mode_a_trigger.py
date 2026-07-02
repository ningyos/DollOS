# tests/test_evolution_mode_a_trigger.py
"""Mode A gate + bookkeeping (spec §3.3). Stub run_evolution_pass; drive the gate."""
import asyncio
import time

import pytest

from dollos.mind import evolution as evo, evolution_trigger as et_mod, self_history
from dollos.mind.evolution_trigger import EvolutionTrigger
from dollos.mind.mind_state import MindState


def _trigger(tmp_path, state=None, **kw):
    state = state or MindState()
    defaults = dict(
        state=state, adapter=None, renderer=None, memsearch=None,
        memory_root=tmp_path, transcripts_root=tmp_path / "tx",
        tool_output_store=None, pack_identity=None, consolidation_trigger=None,
        idle_threshold_s=0, persist_path=tmp_path / "mind_state.json",
        base_interval_days=7.0, max_interval_days=28.0,
        min_history_events=2, min_diary_days=14,
        enforcement=None, floor=80, cap=600)
    defaults.update(kw)
    return EvolutionTrigger(**defaults), state


def _seed_pins(tmp_path, n):
    hist = tmp_path / "self_history.jsonl"
    for i in range(n):
        self_history.log_event(hist, kind="pin_add", turn=i, external_ctx=False,
                               section="self", id=f"s{i}", text=f"條目{i}")
    return hist


def test_init_bootstraps_state(tmp_path):
    trig, state = _trigger(tmp_path)
    assert state.last_evolution_attempt_at > 0
    assert state.evolution_interval_days == 7.0
    assert (tmp_path / "mind_state.json").exists()


def test_mode_a_gate_blocks_each_condition(tmp_path):
    trig, state = _trigger(tmp_path)
    now = state.last_evolution_attempt_at + 8 * 86400
    # material gate empty → blocked
    assert trig._should_run_mode_a(now) is False
    _seed_pins(tmp_path, 2)
    assert trig._should_run_mode_a(now) is True
    # interval not elapsed → blocked
    assert trig._should_run_mode_a(state.last_evolution_attempt_at + 100) is False
    # pending slot (either status) → blocked
    evo.save_slot(tmp_path / "self_evolution" / "pending.json",
                  evo.make_external_slot(candidate="x" * 90, created_ts=0.0))
    assert trig._should_run_mode_a(now) is False


def test_mode_a_bookkeeping_no_change_doubles_and_commits_hwm(tmp_path, monkeypatch):
    trig, state = _trigger(tmp_path)
    hist = _seed_pins(tmp_path, 3)
    now = state.last_evolution_attempt_at + 8 * 86400
    async def fake_pass(**kw):
        return "no_change"
    monkeypatch.setattr(et_mod, "run_evolution_pass", fake_pass)
    asyncio.run(trig._run_mode_a_once(now))
    assert state.evolution_interval_days == 14.0
    assert state.last_evolution_attempt_at == now
    assert state.evolution_hwm == hist.stat().st_size   # verdicted → committed


def test_mode_a_error_sets_cooldown_not_attempt(tmp_path, monkeypatch):
    trig, state = _trigger(tmp_path)
    _seed_pins(tmp_path, 3)
    before = state.last_evolution_attempt_at
    now = before + 8 * 86400
    async def fake_pass(**kw):
        return "error"
    monkeypatch.setattr(et_mod, "run_evolution_pass", fake_pass)
    asyncio.run(trig._run_mode_a_once(now))
    assert state.last_evolution_attempt_at == before     # not an attempt
    assert state.evolution_hwm == 0                      # not committed
    assert trig._should_run_mode_a(now + 10) is False    # 1h cooldown


def test_expire_restores_hwm_and_anchors_attempt(tmp_path):
    from dollos.mind.evolution import surface_or_expire
    state = MindState()
    state.evolution_hwm = 500
    hist = tmp_path / "self_history.jsonl"
    self_history.log_event(hist, kind="evo_adopt", text="現"*90,
                           old_text=None, drift_score=None)
    slot = evo.make_keeper_slot(candidate="新"*90, rationale="r",
                                hwm_before=123, created_ts=0.0)
    slot.surfaced_count = 5
    evo.save_slot(tmp_path / "self_evolution" / "pending.json", slot)
    out = surface_or_expire(
        slot_path=tmp_path / "self_evolution" / "pending.json",
        history_path=hist, current_self_path=tmp_path / "current_self.md",
        sanctioned_text="現"*90, max_surfacings=5, min_age_days=0.0,
        now=999.0, mind_state=state)
    assert out is None
    assert state.evolution_hwm == 123        # restored from hwm_before
    assert state.last_evolution_attempt_at == 999.0
