# tests/test_evolution_mode_a_trigger.py
"""Mode A gate + bookkeeping (spec §3.3). Stub run_evolution_pass; drive the gate."""
import asyncio
import types

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


# --- F6 gate coverage: each remaining Mode-A condition blocks independently ---


def test_mode_a_gate_blocks_on_not_idle(tmp_path):
    """F6(b): condition 1 — recent activity (fresh last_iter_at) blocks Mode A
    even when interval/material/consolidation/slot are all satisfied."""
    trig, state = _trigger(tmp_path, idle_threshold_s=999999)
    _seed_pins(tmp_path, 3)
    now = state.last_evolution_attempt_at + 8 * 86400
    state.last_iter_at = now                     # just active → not idle
    assert trig._should_run_mode_a(now) is False


def test_mode_a_gate_diary_only_material(tmp_path):
    """F6(c): with ZERO pins, the diary-days clause alone opens the material gate
    (spec §3.3 condition 3 is an OR of pin count and diary days)."""
    trig, state = _trigger(tmp_path, min_history_events=999, min_diary_days=1)
    state.last_evolution_attempt_at = 1000.0     # old anchor → 2026 diary dates qualify
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "2026-06-20.md").write_text("## 深夜 日記\n整理日誌", encoding="utf-8")
    (shared / "2026-06-25.md").write_text("## 深夜 日記\n整理日誌", encoding="utf-8")
    now = state.last_evolution_attempt_at + 30 * 86400
    assert evo.count_new_pin_events(tmp_path / "self_history.jsonl", 0) == 0  # no pins
    assert trig._should_run_mode_a(now) is True                              # diary opens it


def test_mode_a_gate_blocks_on_consolidation_running(tmp_path):
    """F6(d): condition 4 — a consolidation keeper in flight blocks Mode A."""
    cons = types.SimpleNamespace(current_task=object())
    trig, state = _trigger(tmp_path, consolidation_trigger=cons)
    _seed_pins(tmp_path, 3)
    now = state.last_evolution_attempt_at + 8 * 86400
    assert trig._should_run_mode_a(now) is False


def test_mode_a_bookkeeping_kill_doubles_and_commits_hwm(tmp_path, monkeypatch):
    """F6(e): §3.3 failure-table skeptic-kill row — interval ×2, HWM committed,
    last_attempt advanced (the evidence was examined; a killed candidate consumes
    it just like no_change)."""
    trig, state = _trigger(tmp_path)
    hist = _seed_pins(tmp_path, 3)
    now = state.last_evolution_attempt_at + 8 * 86400

    async def fake_pass(**kw):
        return "kill"
    monkeypatch.setattr(et_mod, "run_evolution_pass", fake_pass)
    asyncio.run(trig._run_mode_a_once(now))
    assert state.evolution_interval_days == 14.0            # doubled
    assert state.last_evolution_attempt_at == now            # advanced
    assert state.evolution_hwm == hist.stat().st_size        # committed


@pytest.mark.asyncio
async def test_run_loop_launches_mode_a(tmp_path, monkeypatch):
    """F6(a): run()'s poll loop invokes _run_mode_a_once on the Mode-B-priority
    elif branch (_should_reverdict False + _should_run_mode_a True)."""
    trig, state = _trigger(tmp_path)
    trig.POLL_INTERVAL_S = 0.0
    called = {}
    monkeypatch.setattr(trig, "_should_reverdict", lambda now: False)
    monkeypatch.setattr(trig, "_should_run_mode_a", lambda now: True)

    async def fake_mode_a(now):
        called["yes"] = True
        trig._shutdown = True                    # break the poll loop after one launch

    monkeypatch.setattr(trig, "_run_mode_a_once", fake_mode_a)
    await asyncio.wait_for(trig.run(), timeout=1.0)
    assert called.get("yes") is True


def test_mode_a_oserror_anchors_cooldown_and_propagates(tmp_path, monkeypatch):
    """F2: an OSError escaping run_evolution_pass's own handlers (audit-append
    failure, §3.2 never-swallow) propagates to run()'s except Exception — no
    attempt is recorded — but the 1h in-memory cooldown is anchored BEFORE it
    escapes (mirror Mode B M4), so the 5s poll can't retry-loop on failing IO."""
    trig, state = _trigger(tmp_path)
    _seed_pins(tmp_path, 3)
    before = state.last_evolution_attempt_at
    now = before + 8 * 86400

    async def boom(**kw):
        raise OSError("audit append failed")
    monkeypatch.setattr(et_mod, "run_evolution_pass", boom)

    with pytest.raises(OSError):
        asyncio.run(trig._run_mode_a_once(now))

    assert trig._mode_a_error_ts is not None            # cooldown anchored
    assert state.last_evolution_attempt_at == before     # not an attempt
    assert state.evolution_hwm == 0                       # HWM not committed

    # Isolate the cooldown clause: park last_attempt far in the past so the
    # interval gate is always open, leaving the 1h error-cooldown as the only
    # time-dependent blocker near the anchor.
    anchor = trig._mode_a_error_ts
    state.last_evolution_attempt_at = anchor - 100 * 86400
    assert trig._should_run_mode_a(anchor + 10) is False     # within cooldown → blocked
    assert trig._should_run_mode_a(anchor + 3700) is True    # cooldown elapsed → clear


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
