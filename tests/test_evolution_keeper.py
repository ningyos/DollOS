"""Mode A keeper driver (spec §3.3): bundle, parsing, pass outcomes."""
import pytest

from dollos.mind import evolution as evo, evolution_keeper as ek, self_history


VALID = "我最近整理系統日誌時發現自己會安靜下來,那種一行行看下去的踏實感讓我上癮," \
        "我開始主動找這類事情做,不再只是等主人開口才動。" + "細節" * 10


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
    bundle, off = ek.assemble_bundle(memory_root=mr, hwm=0, window_days=28.0)
    assert "pin_add" in bundle and "日記" in bundle and "主人偏好深夜工作" in bundle
    assert off == hist.stat().st_size


def test_assemble_bundle_truncation_drops_consolidated_first(tmp_path):
    mr, hist = _seed_root(tmp_path)
    big = "x" * 3000
    (mr / "consolidated" / "2026-06-29.md").write_text(big, encoding="utf-8")
    bundle, _ = ek.assemble_bundle(memory_root=mr, hwm=0, window_days=28.0,
                                   budget_chars=800)
    assert "pin_add" in bundle            # self_history survives (dropped last)
    assert big not in bundle              # consolidated sacrificed first


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


def _pass_kwargs(mr):
    from dollos.character import Enforcement, Identity
    ident = Identity(self="我是測試角色", personality="安靜", taboos="不編造")
    return dict(adapter=None, renderer=None, memsearch=None, memory_root=mr,
                transcripts_root=mr / "tx", tool_output_store=None,
                pack_identity=ident, enforcement=Enforcement(),
                floor=80, cap=600, max_tokens=2048, now=1000.0)
