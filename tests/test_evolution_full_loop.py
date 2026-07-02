"""Plan-3 full loop: pins → Mode A pass → keeper slot → surfacing → adopt (spec §6).

Stubs ONLY the LLM boundary (``evolution_keeper._run_keeper_agent`` /
``_run_full_skeptic``); everything else — ``assemble_bundle``, mechanical
checks, slot persistence, ``surface_or_expire``, and ``SelfRevision`` — runs
for real, driven end-to-end through real files under ``tmp_path``.
"""
import types

import pytest

from dollos.character import Enforcement, Identity
from dollos.mind import evolution as evo
from dollos.mind import evolution_keeper as ek
from dollos.mind import self_history
from dollos.mind.mind_state import MindState
from dollos.tools import SelfRevision

VALID = ("我最近發現自己會主動整理系統日誌,一行行看下去有種踏實感,不再只是等主人開口才動,"
         "遇到看不懂的紀錄還會自己追下去查清楚才安心。" + "細節" * 9)   # 81 chars ≥ floor 80


def _identity() -> Identity:
    return Identity(self="我是測試角色", personality="安靜", taboos="不編造")


@pytest.mark.asyncio
async def test_full_loop_candidate_to_adopt(tmp_path, monkeypatch):
    mr = tmp_path
    hist = mr / "self_history.jsonl"
    for i in range(8):
        self_history.log_event(hist, kind="pin_add", turn=i, external_ctx=False,
                               section="self", id=f"s{i}", text=f"喜歡整理日誌{i}")

    async def fake_keeper(**kw):
        assert "喜歡整理日誌0" in kw["task"]          # bundle actually reached the keeper
        return {"details": f"CANDIDATE\n{VALID}\n依據:\n- s0 存活多週"}

    async def fake_skeptic(**kw):
        assert kw["bundle"]                            # byte-identical bundle forwarded
        return "pass"

    monkeypatch.setattr(ek, "_run_keeper_agent", fake_keeper)
    monkeypatch.setattr(ek, "_run_full_skeptic", fake_skeptic)

    out = await ek.run_evolution_pass(
        adapter=None, renderer=None, memsearch=None, memory_root=mr,
        transcripts_root=mr / "tx", tool_output_store=None, pack_identity=_identity(),
        enforcement=Enforcement(), floor=80, cap=600, max_tokens=2048,
        now=1000.0, hwm=0)
    assert out == "candidate"

    # Surface → adopt via the REAL Plan-2 machinery.
    slot_path = mr / "self_evolution" / "pending.json"
    block = evo.surface_or_expire(
        slot_path=slot_path, history_path=hist,
        current_self_path=mr / "current_self.md", sanctioned_text=None,
        max_surfacings=5, min_age_days=2.0, now=2000.0, mind_state=MindState())
    assert block is not None and "[人格演化候選]" in block and VALID in block

    ctx = types.SimpleNamespace(
        memory_root=mr, evolution_latched=False, evolution_candidate_surfaced=True,
        enforcement=Enforcement(), current_self_min_chars=80, current_self_max_chars=600,
        mind_state=MindState(), evolution_base_interval_days=7.0,
        evolution_max_interval_days=28.0)
    result = await SelfRevision(decision="adopt", text="", reason="").run(ctx)
    assert "採納" in result
    assert self_history.sanctioned_text(hist) == VALID
    assert (mr / "current_self.md").read_text(encoding="utf-8") == VALID
    assert ctx.mind_state.evolution_interval_days == 7.0   # reset on adopt


@pytest.mark.asyncio
async def test_full_loop_no_change_starves_quietly(tmp_path, monkeypatch):
    """Keeper finds no coherent shift → NO_CHANGE: no slot, evo_no_change
    logged, skeptic never runs (寧缺勿濫 — the pass ends before the
    mechanical/skeptic gates it would need a candidate to reach)."""
    mr = tmp_path

    async def fake_keeper(**kw):
        return {"details": "NO_CHANGE 沒有足夠證據支持任何調整"}

    async def fake_skeptic(**kw):
        raise AssertionError("skeptic must not run on the no_change path")

    monkeypatch.setattr(ek, "_run_keeper_agent", fake_keeper)
    monkeypatch.setattr(ek, "_run_full_skeptic", fake_skeptic)

    out = await ek.run_evolution_pass(
        adapter=None, renderer=None, memsearch=None, memory_root=mr,
        transcripts_root=mr / "tx", tool_output_store=None, pack_identity=_identity(),
        enforcement=Enforcement(), floor=80, cap=600, max_tokens=2048,
        now=1000.0, hwm=0)
    assert out == "no_change"

    slot_path = mr / "self_evolution" / "pending.json"
    assert not slot_path.exists()
    hist = mr / "self_history.jsonl"
    events = self_history.read_events(hist)
    assert [e["kind"] for e in events] == [evo.EVO_NO_CHANGE]
    assert events[-1]["reason"] == "沒有足夠證據支持任何調整"


@pytest.mark.asyncio
async def test_full_loop_skeptic_kill_no_slot(tmp_path, monkeypatch):
    """A candidate that clears mechanical checks but fails the full-scope
    (a)-(e) skeptic never creates a slot — evo_kill logged, nothing to
    surface."""
    mr = tmp_path

    async def fake_keeper(**kw):
        return {"details": f"CANDIDATE\n{VALID}\n依據:\n- s0 存活多週"}

    async def fake_skeptic(**kw):
        return "kill:(e) 依據引用的事件在紀錄裡找不到"

    monkeypatch.setattr(ek, "_run_keeper_agent", fake_keeper)
    monkeypatch.setattr(ek, "_run_full_skeptic", fake_skeptic)

    out = await ek.run_evolution_pass(
        adapter=None, renderer=None, memsearch=None, memory_root=mr,
        transcripts_root=mr / "tx", tool_output_store=None, pack_identity=_identity(),
        enforcement=Enforcement(), floor=80, cap=600, max_tokens=2048,
        now=1000.0, hwm=0)
    assert out == "kill"

    slot_path = mr / "self_evolution" / "pending.json"
    assert not slot_path.exists()
    hist = mr / "self_history.jsonl"
    last = self_history.read_events(hist)[-1]
    assert last["kind"] == evo.EVO_KILL
    assert last["reason"] == "(e) 依據引用的事件在紀錄裡找不到"
