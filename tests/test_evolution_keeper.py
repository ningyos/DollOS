"""Mode A keeper driver (spec §3.3): bundle, parsing, pass outcomes."""
from datetime import datetime

import pytest

from dollos.mind import evolution as evo, evolution_keeper as ek, self_history


VALID = "我最近整理系統日誌時發現自己會安靜下來,那種一行行看下去的踏實感讓我上癮," \
        "我開始主動找這類事情做,不再只是等主人開口才動。" + "細節" * 10

# Hermetic clock (review Important — single injected clock): the seeded
# 2026-06/07 fixture dates sit inside NOW's 28-day window forever.
NOW = datetime(2026, 7, 2, 12, 0).timestamp()


def _seed_root(tmp_path):
    mr = tmp_path
    hist = mr / "self_history.jsonl"
    self_history.log_event(hist, kind="pin_add", turn=1, external_ctx=False,
                           section="self", id="s1", text="喜歡整理日誌")
    (mr / "shared").mkdir(exist_ok=True)
    (mr / "shared" / "2026-07-01.md").write_text("## 深夜 日記\n今天整理了日誌,很平靜。",
                                                 encoding="utf-8")
    (mr / "consolidated").mkdir(exist_ok=True)
    (mr / "consolidated" / "2026-06-30.md").write_text("- 主人偏好深夜工作",
                                                       encoding="utf-8")
    return mr, hist


def test_assemble_bundle_contains_all_classes_and_returns_offset(tmp_path):
    mr, hist = _seed_root(tmp_path)
    bundle, off = ek.assemble_bundle(memory_root=mr, hwm=0, window_days=28.0,
                                     now=NOW)
    assert "pin_add" in bundle and "日記" in bundle and "主人偏好深夜工作" in bundle
    assert off == hist.stat().st_size


def test_assemble_bundle_truncation_drops_consolidated_first(tmp_path):
    mr, hist = _seed_root(tmp_path)
    big = "x" * 3000
    (mr / "consolidated" / "2026-06-29.md").write_text(big, encoding="utf-8")
    bundle, _ = ek.assemble_bundle(memory_root=mr, hwm=0, window_days=28.0,
                                   budget_chars=800, now=NOW)
    assert "pin_add" in bundle            # self_history survives (dropped last)
    assert big not in bundle              # consolidated sacrificed first


def test_assemble_bundle_truncation_drops_oldest_diary_when_no_consolidated(tmp_path):
    """§6 gap: with consolidated exhausted, the oldest DIARY is sacrificed
    next; fixed (self_history) always survives."""
    mr, hist = _seed_root(tmp_path)
    (mr / "consolidated" / "2026-06-30.md").unlink()   # consolidated empty
    big = "## 日記\n" + "y" * 3000
    (mr / "shared" / "2026-06-20.md").write_text(big, encoding="utf-8")
    bundle, _ = ek.assemble_bundle(memory_root=mr, hwm=0, window_days=28.0,
                                   budget_chars=800, now=NOW)
    assert "pin_add" in bundle            # fixed survives (dropped last)
    assert "y" * 3000 not in bundle       # oldest diary sacrificed first
    assert "今天整理了日誌" in bundle       # newer diary survives


def test_parse_keeper_report_no_change():
    kind, text, rationale = ek.parse_keeper_report("NO_CHANGE 證據不足,沒有連貫的變化")
    assert kind == "no_change" and "證據不足" in text


def test_parse_keeper_report_candidate():
    details = f"CANDIDATE\n{VALID}\n依據:\n- s1 喜歡整理日誌 存活多週\n- 日記反覆出現"
    kind, text, rationale = ek.parse_keeper_report(details)
    assert kind == "candidate" and text == VALID and "存活多週" in rationale


def test_parse_keeper_report_malformed_raises():
    with pytest.raises(ValueError):
        ek.parse_keeper_report("")


@pytest.mark.asyncio
async def test_pass_no_change_logs_and_returns(tmp_path, monkeypatch):
    mr, hist = _seed_root(tmp_path)
    async def fake_keeper(**kw):
        return {"details": "NO_CHANGE 證據不足"}
    monkeypatch.setattr(ek, "_run_keeper_agent", fake_keeper)
    out = await ek.run_evolution_pass(**_pass_kwargs(mr))
    assert out == "no_change"
    assert [e["kind"] for e in self_history.read_events(hist)][-1] == "evo_no_change"
    assert evo.load_slot(mr / "self_evolution" / "pending.json") is None


@pytest.mark.asyncio
async def test_pass_candidate_creates_awaiting_doll_slot_with_hwm(tmp_path, monkeypatch):
    mr, hist = _seed_root(tmp_path)
    async def fake_keeper(**kw):
        return {"details": f"CANDIDATE\n{VALID}\n依據:\n- s1 存活"}
    async def fake_skeptic(**kw):
        return "pass"
    monkeypatch.setattr(ek, "_run_keeper_agent", fake_keeper)
    monkeypatch.setattr(ek, "_run_full_skeptic", fake_skeptic)
    out = await ek.run_evolution_pass(**_pass_kwargs(mr))
    assert out == "candidate"
    slot = evo.load_slot(mr / "self_evolution" / "pending.json")
    assert slot.kind == "keeper" and slot.status == "awaiting_doll"
    assert slot.candidate == VALID and slot.hwm_before == 0   # pre-snapshot offset
    kinds = [e["kind"] for e in self_history.read_events(hist)]
    assert kinds[-1] == "evo_candidate"


@pytest.mark.asyncio
async def test_pass_skeptic_receives_byte_identical_bundle(tmp_path, monkeypatch):
    """§6 gap: the (a)-(e) skeptic must see the EXACT bundle the keeper saw —
    recomputed here with the pass's own args, and cross-checked as a substring
    of the keeper task. Also pins the floor/cap prompt interpolation."""
    mr, hist = _seed_root(tmp_path)
    kwargs = _pass_kwargs(mr)
    expected_bundle, _ = ek.assemble_bundle(
        memory_root=mr, hwm=0, window_days=28.0, now=kwargs["now"])
    seen = {}
    async def fake_keeper(**kw):
        seen["keeper_task"] = kw["task"]
        return {"details": f"CANDIDATE\n{VALID}\n依據:\n- s1 存活"}
    async def fake_skeptic(**kw):
        seen["skeptic_bundle"] = kw["bundle"]
        return "pass"
    monkeypatch.setattr(ek, "_run_keeper_agent", fake_keeper)
    monkeypatch.setattr(ek, "_run_full_skeptic", fake_skeptic)
    out = await ek.run_evolution_pass(**kwargs)
    assert out == "candidate"
    assert seen["skeptic_bundle"] == expected_bundle   # byte-identical
    assert expected_bundle in seen["keeper_task"]      # same bytes keeper saw
    assert "80–600 字" in seen["keeper_task"]          # configured bounds interpolated


@pytest.mark.asyncio
async def test_pass_mechanical_kill(tmp_path, monkeypatch):
    mr, hist = _seed_root(tmp_path)
    async def fake_keeper(**kw):
        return {"details": "CANDIDATE\n太短\n依據:\n- x"}
    monkeypatch.setattr(ek, "_run_keeper_agent", fake_keeper)
    out = await ek.run_evolution_pass(**_pass_kwargs(mr))
    assert out == "kill"
    events = self_history.read_events(hist)
    assert events[-1]["kind"] == "evo_kill" and "mechanical" in events[-1]["reason"]


@pytest.mark.asyncio
async def test_pass_skeptic_kill(tmp_path, monkeypatch):
    mr, hist = _seed_root(tmp_path)
    async def fake_keeper(**kw):
        return {"details": f"CANDIDATE\n{VALID}\n依據:\n- s1"}
    async def fake_skeptic(**kw):
        return "kill:引用的證據不存在"
    monkeypatch.setattr(ek, "_run_keeper_agent", fake_keeper)
    monkeypatch.setattr(ek, "_run_full_skeptic", fake_skeptic)
    out = await ek.run_evolution_pass(**_pass_kwargs(mr))
    assert out == "kill"
    assert evo.load_slot(mr / "self_evolution" / "pending.json") is None


@pytest.mark.asyncio
async def test_pass_keeper_error_returns_error_and_logs(tmp_path, monkeypatch):
    mr, hist = _seed_root(tmp_path)
    async def boom(**kw):
        raise RuntimeError("llm down")
    monkeypatch.setattr(ek, "_run_keeper_agent", boom)
    out = await ek.run_evolution_pass(**_pass_kwargs(mr))
    assert out == "error"
    assert [e["kind"] for e in self_history.read_events(hist)][-1] == "evo_error"


@pytest.mark.asyncio
async def test_pass_yields_to_slot_created_mid_pass(tmp_path, monkeypatch):
    """Task-4 review Minor 1: while the keeper/skeptic LLM calls are in flight,
    a background-perception turn's tripwire can create an external
    awaiting_skeptic slot. The pass must NOT clobber it — it yields: evo_kill
    (superseded reason), NO evo_candidate birth line, external slot untouched."""
    mr, hist = _seed_root(tmp_path)
    slot_path = mr / "self_evolution" / "pending.json"
    external_candidate = "有人在 pass 進行中改了檔案。" + "字" * 80

    async def fake_keeper(**kw):
        return {"details": f"CANDIDATE\n{VALID}\n依據:\n- s1 存活"}

    async def fake_skeptic(**kw):
        # Simulate the mid-await interleave: the tripwire lands an external
        # slot while the skeptic LLM call is still in flight, then passes.
        evo.save_slot(slot_path, evo.make_external_slot(
            candidate=external_candidate, created_ts=50.0))
        return "pass"

    monkeypatch.setattr(ek, "_run_keeper_agent", fake_keeper)
    monkeypatch.setattr(ek, "_run_full_skeptic", fake_skeptic)
    out = await ek.run_evolution_pass(**_pass_kwargs(mr))

    assert out == "kill"
    survivor = evo.load_slot(slot_path)
    assert survivor is not None and survivor.kind == "external"      # untouched
    assert survivor.candidate == external_candidate
    assert survivor.status == "awaiting_skeptic"
    events = self_history.read_events(hist)
    assert events[-1]["kind"] == "evo_kill"
    assert events[-1]["reason"] == "superseded:slot_created_mid_pass"
    assert "evo_candidate" not in [e["kind"] for e in events]        # no birth line


def _pass_kwargs(mr):
    from dollos.character import Enforcement, Identity
    ident = Identity(self="我是測試角色", personality="安靜", taboos="不編造")
    return dict(adapter=None, renderer=None, memsearch=None, memory_root=mr,
                transcripts_root=mr / "tx", tool_output_store=None,
                pack_identity=ident, enforcement=Enforcement(),
                floor=80, cap=600, max_tokens=2048, now=1000.0)
