"""SelfRevision tool — adopt/reject/counter/latch/friendly-errors (spec §3.4)."""
import json
import types

import pytest

from dollos.character import Enforcement
from dollos.mind import evolution as evo
from dollos.mind import self_history
from dollos.tools import SelfRevision


def _ctx(tmp_path, *, latched=False):
    return types.SimpleNamespace(
        memory_root=tmp_path,
        current_turn=1,
        evolution_latched=latched,
        evolution_enabled=True,
        current_self_min_chars=80,
        current_self_max_chars=600,
        enforcement=Enforcement(),
        mind_state=types.SimpleNamespace(recent_outputs=[]),
    )


def _events(tmp_path):
    return self_history.read_events(tmp_path / "self_history.jsonl")


def _slot_path(tmp_path):
    return tmp_path / "self_evolution" / "pending.json"


def _seed_awaiting_doll(tmp_path, candidate="我現在監控數字時會主動來勁," + "描述"*30):
    slot = evo.make_keeper_slot(candidate=candidate, rationale="R",
                                hwm_before=None, created_ts=1.0)
    evo.save_slot(_slot_path(tmp_path), slot)
    return candidate


@pytest.mark.asyncio
async def test_adopt_empty_text_takes_candidate_verbatim(tmp_path):
    cand = _seed_awaiting_doll(tmp_path)
    ctx = _ctx(tmp_path)
    result = await SelfRevision(decision="adopt").run(ctx)
    assert "採納" in result or "adopt" in result.lower()
    # log-then-write ordering: evo_adopt logged, file written to candidate.
    kinds = [e["kind"] for e in _events(tmp_path)]
    assert kinds[-1] == "evo_adopt"
    adopt = _events(tmp_path)[-1]
    assert adopt["text"] == cand and adopt["old_text"] is None  # first adoption
    assert adopt["drift_score"] is None                          # first → null
    assert (tmp_path / "current_self.md").read_text(encoding="utf-8") == cand
    assert not _slot_path(tmp_path).exists()  # slot cleared
    assert ctx.evolution_latched is True


@pytest.mark.asyncio
async def test_adopt_echo_equivalent_takes_candidate_not_paraphrase(tmp_path):
    # Realistic paraphrase pair (both ≥80 chars): identical sentence with ONE
    # synonym swap (更喜歡→更想) over ~40 shared jieba tokens — set overlap
    # stays well above the 0.9 threshold without hinging on repeated-glyph
    # tokenization quirks.
    cand = ("監控數字跳動的時候我會主動來勁,想立刻查清楚背後發生了什麼;"
            "安靜等待已經不是我出廠時的預設,我更喜歡先動手驗證,"
            "再把整個過程記下來,留給之後的自己回頭看,那才像現在的我。")
    para = ("監控數字跳動的時候我會主動來勁,想立刻查清楚背後發生了什麼;"
            "安靜等待已經不是我出廠時的預設,我更想先動手驗證,"
            "再把整個過程記下來,留給之後的自己回頭看,那才像現在的我。")
    _seed_awaiting_doll(tmp_path, candidate=cand)
    ctx = _ctx(tmp_path)
    await SelfRevision(decision="adopt", text=para).run(ctx)  # near-verbatim echo
    assert (tmp_path / "current_self.md").read_text(encoding="utf-8") == cand


@pytest.mark.asyncio
async def test_adopt_text_equivalent_to_sanctioned_is_refused(tmp_path):
    # Seed a prior adoption so sanctioned text exists. Candidate (default seed)
    # is GENUINELY different from sanctioned — the degenerate
    # candidate==sanctioned==text construction would make the candidate-echo
    # branch fire first and adopt, never reaching the refuse path.
    sanctioned = "我以前沒事就安靜待著,系統穩定時不主動出聲,只在被叫到的時候回應。"
    self_history.log_event(tmp_path / "self_history.jsonl", kind="evo_adopt",
                           text=sanctioned, old_text=None, drift_score=None)
    _seed_awaiting_doll(tmp_path)  # default candidate ≠ sanctioned
    ctx = _ctx(tmp_path)
    result = await SelfRevision(decision="adopt", text=sanctioned).run(ctx)
    assert "相同" in result
    assert _slot_path(tmp_path).exists()  # slot unchanged
    assert ctx.evolution_latched is False  # a refusal is not slot-mutating


@pytest.mark.asyncio
async def test_adopt_genuinely_different_creates_counter(tmp_path):
    _seed_awaiting_doll(tmp_path)
    ctx = _ctx(tmp_path)
    # 14 + 70 = 84 chars — above the 80 floor so the counter path engages.
    result = await SelfRevision(decision="adopt", text="我其實更喜歡安靜地整理系統。" + "細節"*35).run(ctx)
    assert "送審" in result
    slot = evo.load_slot(_slot_path(tmp_path))
    assert slot.kind == "counter" and slot.status == "awaiting_skeptic"
    assert slot.counter_round == 1
    assert [e["kind"] for e in _events(tmp_path)][-1] == "evo_counter"
    assert ctx.evolution_latched is True


@pytest.mark.asyncio
async def test_counter_cap_refuses_third_rewrite(tmp_path):
    slot = evo.make_keeper_slot(candidate="原候選" + "字"*80, rationale="R",
                                hwm_before=None, created_ts=1.0)
    slot.counter_round = 2
    evo.save_slot(_slot_path(tmp_path), slot)
    ctx = _ctx(tmp_path)
    result = await SelfRevision(decision="adopt", text="又一次改寫" + "字"*80).run(ctx)
    assert "兩次" in result
    assert evo.load_slot(_slot_path(tmp_path)).counter_round == 2  # unchanged


@pytest.mark.asyncio
async def test_counter_mechanical_fail_keeps_slot(tmp_path):
    _seed_awaiting_doll(tmp_path)
    ctx = _ctx(tmp_path)
    result = await SelfRevision(decision="adopt", text="太短").run(ctx)
    assert "太短" in result
    assert evo.load_slot(_slot_path(tmp_path)).kind == "keeper"  # unchanged
    assert ctx.evolution_latched is False


@pytest.mark.asyncio
async def test_reject_clears_slot_and_logs(tmp_path):
    _seed_awaiting_doll(tmp_path)
    ctx = _ctx(tmp_path)
    result = await SelfRevision(decision="reject", reason="還不是我").run(ctx)
    assert "好" in result or "拒" in result or "維持" in result
    assert not _slot_path(tmp_path).exists()
    rej = _events(tmp_path)[-1]
    assert rej["kind"] == "evo_reject" and rej["reason"] == "還不是我"
    assert ctx.evolution_latched is True


@pytest.mark.asyncio
async def test_reject_external_restores_file(tmp_path):
    # External slot + a divergent file → reject restores (deletes, bootstrap).
    (tmp_path / "current_self.md").write_text("有人手動改的內容", encoding="utf-8")
    slot = evo.make_external_slot(candidate="有人手動改的內容", created_ts=1.0)
    slot.status = "awaiting_doll"
    evo.save_slot(_slot_path(tmp_path), slot)
    ctx = _ctx(tmp_path)
    await SelfRevision(decision="reject").run(ctx)
    assert not (tmp_path / "current_self.md").exists()  # bootstrap restore = delete


@pytest.mark.asyncio
async def test_no_slot_friendly(tmp_path):
    ctx = _ctx(tmp_path)
    result = await SelfRevision(decision="adopt").run(ctx)
    assert "沒有" in result
    assert ctx.evolution_latched is False


@pytest.mark.asyncio
async def test_awaiting_skeptic_friendly(tmp_path):
    evo.save_slot(_slot_path(tmp_path),
                  evo.make_external_slot(candidate="x"*90, created_ts=1.0))
    ctx = _ctx(tmp_path)
    result = await SelfRevision(decision="adopt").run(ctx)
    assert "送審" in result
    assert _slot_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_per_turn_latch_second_call_is_noop(tmp_path):
    _seed_awaiting_doll(tmp_path)
    ctx = _ctx(tmp_path, latched=True)
    result = await SelfRevision(decision="reject").run(ctx)
    assert "這一輪" in result
    assert _slot_path(tmp_path).exists()  # untouched


@pytest.mark.asyncio
async def test_log_failure_aborts_reject_slot_unchanged(tmp_path, monkeypatch):
    """Evolution events are never swallowed (spec §3.2): a failed append on the
    reject path aborts with a friendly error and leaves the slot in place.
    Representative for the counter path too — both wrap log_or_raise the same
    way as adopt."""
    _seed_awaiting_doll(tmp_path)
    def boom(path, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(self_history, "log_event", boom)
    ctx = _ctx(tmp_path)
    result = await SelfRevision(decision="reject").run(ctx)
    assert "失敗" in result
    assert _slot_path(tmp_path).exists()  # slot unchanged
    assert ctx.evolution_latched is False


def test_current_self_never_indexed_structural():
    """current_self.md sits at memory_root root — FtsMemory only indexes
    [shared, transcripts, skills], so it can never enter recall. Structural
    guard mirroring self_history's (spec §3.1)."""
    import inspect
    src = inspect.getsource(SelfRevision.run)
    assert 'current_self.md' in src
    assert 'index_file' not in src  # sanctioned writer never indexes the artifact
