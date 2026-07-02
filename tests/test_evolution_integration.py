"""Full ratification paths end-to-end (spec §6 acceptance, stub LLM)."""
import types

import pytest

from dollos.character import Enforcement, Identity
from dollos.mind import current_self, evolution as evo, self_history
from dollos.mind.evolution_trigger import EvolutionTrigger
from dollos.mind.mind_prompt import render_mind
from dollos.mind.mind_state import MindState
from dollos.tools import SelfRevision


def _ctx(tmp_path):
    return types.SimpleNamespace(
        memory_root=tmp_path, current_turn=1, evolution_latched=False,
        evolution_enabled=True, current_self_min_chars=80,
        current_self_max_chars=600, enforcement=Enforcement(),
        mind_state=types.SimpleNamespace(recent_outputs=[]))


@pytest.mark.asyncio
async def test_external_edit_ratification_end_to_end(tmp_path, monkeypatch):
    hist = tmp_path / "self_history.jsonl"
    cs = tmp_path / "current_self.md"
    sp = tmp_path / "self_evolution" / "pending.json"
    edited = "我現在其實更喜歡在夜裡慢慢整理系統日誌。" + "細節" * 30

    # 1. user hand-edits the file → tripwire detects → external awaiting_skeptic slot.
    cs.write_text(edited, encoding="utf-8")
    evo.process_tripwire(current_self_path=cs, history_path=hist, slot_path=sp,
                         enforcement=Enforcement(), floor=80, cap=600, now=1.0)
    assert evo.load_slot(sp).status == "awaiting_skeptic"

    # 2. Mode-B skeptic passes → awaiting_doll.
    trig = EvolutionTrigger(
        state=types.SimpleNamespace(last_user_at=0.0, last_iter_at=0.0),
        adapter=object(), renderer=object(), memsearch=object(),
        memory_root=tmp_path, transcripts_root=tmp_path, tool_output_store=object(),
        pack_identity=Identity(self="You are Gura.", personality="p", taboos="t"),
        consolidation_trigger=types.SimpleNamespace(current_task=None))
    async def _pass(**kw): return "pass"
    monkeypatch.setattr(trig, "_skeptic", _pass)
    await trig._reverdict_once()
    assert evo.load_slot(sp).status == "awaiting_doll"

    # 3. surfacing uses neutral attribution.
    block = evo.render_surfacing(slot=evo.load_slot(sp), sanctioned_text=None, reminder_n=1)
    assert "無法確認是誰" in block

    # 4. Doll adopts → sanctioned = edited, file kept, slot cleared, generation bumps.
    await SelfRevision(decision="adopt").run(_ctx(tmp_path))
    assert self_history.sanctioned_text(hist) == edited
    assert self_history.generation(hist) == 1
    assert cs.read_text(encoding="utf-8") == edited
    assert evo.load_slot(sp) is None

    # 5. renders next turn without restart (composition reads sanctioned from log).
    section = current_self.render_section(self_history.sanctioned_text(hist))
    assert "## 現在的我" in section and "夜裡慢慢整理系統日誌" in section


@pytest.mark.asyncio
async def test_counter_round_trip_then_adopt(tmp_path, monkeypatch):
    hist = tmp_path / "self_history.jsonl"
    sp = tmp_path / "self_evolution" / "pending.json"
    # keeper candidate awaiting_doll (Plan 3 makes these; here we seed directly).
    evo.save_slot(sp, evo.make_keeper_slot(candidate="候選:安靜。" + "字" * 80,
                                           rationale="R", hwm_before=None, created_ts=1.0))
    # 17 + 66 = 83 chars — above the 80 floor so the counter path engages.
    my_rewrite = "我的改寫:我其實是主動來勁的那種。" + "細節" * 33

    # Doll counters.
    await SelfRevision(decision="adopt", text=my_rewrite).run(_ctx(tmp_path))
    assert evo.load_slot(sp).status == "awaiting_skeptic" and evo.load_slot(sp).kind == "counter"

    # skeptic passes → awaiting_doll, surfaces "已通過".
    trig = EvolutionTrigger(
        state=types.SimpleNamespace(last_user_at=0.0, last_iter_at=0.0),
        adapter=object(), renderer=object(), memsearch=object(),
        memory_root=tmp_path, transcripts_root=tmp_path, tool_output_store=object(),
        pack_identity=Identity(self="s", personality="p", taboos="t"),
        consolidation_trigger=types.SimpleNamespace(current_task=None))
    async def _pass(**kw): return "pass"
    monkeypatch.setattr(trig, "_skeptic", _pass)
    await trig._reverdict_once()
    assert evo.load_slot(sp).status == "awaiting_doll"

    # Doll adopts her (now-verdicted) rewrite verbatim.
    await SelfRevision(decision="adopt").run(_ctx(tmp_path))
    assert self_history.sanctioned_text(hist) == my_rewrite


@pytest.mark.asyncio
async def test_reject_restores_and_leaves_no_slot(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    cs = tmp_path / "current_self.md"
    sp = tmp_path / "self_evolution" / "pending.json"
    cs.write_text("有人亂改的內容。" + "字" * 80, encoding="utf-8")
    slot = evo.make_external_slot(candidate="有人亂改的內容。" + "字" * 80, created_ts=1.0)
    slot.status = "awaiting_doll"
    evo.save_slot(sp, slot)
    await SelfRevision(decision="reject", reason="不是我").run(_ctx(tmp_path))
    assert evo.load_slot(sp) is None
    assert not cs.exists()  # slot-resolution invariant (bootstrap delete)
    assert self_history.read_events(hist)[-1]["kind"] == "evo_reject"


@pytest.mark.asyncio
async def test_adopt_log_failure_writes_nothing(tmp_path, monkeypatch):
    """Direct proof of the §3.2 log-then-write invariant on the identity
    path (controller carry-item, Task 6/8 review): a failed history append
    during adopt must leave current_self.md, the sanctioned text, and the
    slot completely untouched. test_self_revision.py already covers this
    for reject (test_log_failure_aborts_reject_slot_unchanged, noting it's
    "representative for the counter path too") but explicitly defers the
    adopt/identity-writing path — this is that direct proof, seeded with a
    PRIOR sanctioned adoption so a regression that overwrites on failure is
    actually observable (not just "stays empty")."""
    hist = tmp_path / "self_history.jsonl"
    cs = tmp_path / "current_self.md"
    sp = tmp_path / "self_evolution" / "pending.json"
    old_text = "舊的現在的我描述。" + "字" * 80
    new_candidate = "新的候選改寫版本。" + "細節" * 40

    # Prior sanctioned adoption — real write, BEFORE the failure is injected.
    self_history.log_event(hist, kind=evo.EVO_ADOPT, text=old_text,
                           old_text=None, drift_score=None)
    cs.write_text(old_text, encoding="utf-8")

    # A fresh awaiting_doll slot pending adoption.
    evo.save_slot(sp, evo.make_external_slot(candidate=new_candidate, created_ts=1.0))
    slot = evo.load_slot(sp)
    slot.status = "awaiting_doll"
    evo.save_slot(sp, slot)

    def boom(path, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(self_history, "log_event", boom)

    ctx = _ctx(tmp_path)
    result = await SelfRevision(decision="adopt").run(ctx)

    assert "失敗" in result
    assert self_history.sanctioned_text(hist) == old_text  # unchanged
    assert cs.read_text(encoding="utf-8") == old_text       # unchanged
    assert evo.load_slot(sp) is not None                    # slot still there
    assert ctx.evolution_latched is False


def test_render_mind_places_evolution_block_after_self_profile():
    """Controller carry-item (Task 6/8 review): the [人格演化候選] block must
    render after [Self profile] and before [Memory guideline] so it stays
    salient on reflection turns without displacing the always-inject self
    block (mind_prompt.render_mind docstring, spec §3.4)."""
    state = MindState()
    self_profile_body = "## 我學到的自己\n- [s1·2026-07-02] 我重視誠實"
    evolution_body = "[人格演化候選]\n（第 1 次提醒)\n舊：(尚無)\n新：候選文字"
    out = render_mind(state, [], "SYSTEM", self_profile_text=self_profile_body,
                       evolution_block=evolution_body)
    assert (out.index("[Self profile]")
            < out.index("[人格演化候選]")
            < out.index("[Memory guideline]"))
