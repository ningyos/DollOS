"""Mode A keeper driver (spec §3.3): bundle, parsing, pass outcomes."""
from datetime import datetime

import pytest

from dollos.mind import evolution as evo, evolution_keeper as ek, self_history


VALID = ("Lately I've noticed I go quiet when I sort through system logs; reading them "
         "line by line steadies me, and I've started looking for that kind of work "
         "myself instead of waiting for my owner.")

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
    assert "pin_add" in bundle and "主人偏好深夜工作" in bundle
    assert off == hist.stat().st_size


def test_assemble_bundle_labels_are_english(tmp_path):
    """All-English plan Task 5: the daemon's labels around her record are
    English (her old Chinese files inside them stay as written)."""
    mr, _hist = _seed_root(tmp_path)
    bundle, _ = ek.assemble_bundle(memory_root=mr, hwm=0, window_days=28.0, now=NOW)
    assert "[self_history events]" in bundle
    assert "[Diary 2026-07-01]" in bundle
    assert "[consolidated · secondary 2026-06-30]" in bundle


@pytest.mark.parametrize("heading,is_diary", [
    ("## 2026-07-01 23:10:00 [mood:calm] Diary", True),    # what WriteDiary writes now
    ("## 2026-07-01 23:10:00 [mood:calm] 日記", True),     # old files
    ("## 日記 (12:34:56)", True),                           # oldest heading format
    ("## 12:00", False),                                    # a note heading
])
def test_assemble_bundle_reads_english_and_old_chinese_diaries(tmp_path, heading, is_diary):
    """All-English plan Task 3: a day file is diary evidence by its heading,
    English or old Chinese; a plain note is not."""
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "2026-07-01.md").write_text(
        f"{heading}\nquiet night sorting logs", encoding="utf-8")
    bundle, _ = ek.assemble_bundle(memory_root=tmp_path, hwm=0, window_days=28.0,
                                   now=NOW)
    assert ("quiet night sorting logs" in bundle) is is_diary


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
    kind, text, rationale = ek.parse_keeper_report(
        "NO_CHANGE: not enough evidence, no coherent shift")
    assert kind == "no_change" and text == "not enough evidence, no coherent shift"


def test_parse_keeper_report_bare_no_change_has_english_default_reason():
    assert ek.parse_keeper_report("NO_CHANGE") == ("no_change", "not enough evidence", "")


def test_parse_keeper_report_candidate():
    details = (f"CANDIDATE\n{VALID}\nEvidence:\n- s1 likes sorting logs, survived weeks"
               "\n- recurs in the diary")
    kind, text, rationale = ek.parse_keeper_report(details)
    assert kind == "candidate" and text == VALID
    assert rationale == "- s1 likes sorting logs, survived weeks\n- recurs in the diary"


def test_parse_keeper_report_empty_raises():
    with pytest.raises(ValueError):
        ek.parse_keeper_report("")


def test_parse_keeper_report_prefixless_but_evidence_line_is_candidate():
    """F4: a weak model that dropped the CANDIDATE prefix but kept the
    Evidence structure still parses as a candidate (structure present)."""
    details = f"{VALID}\nEvidence:\n- s1 survived weeks"
    kind, text, rationale = ek.parse_keeper_report(details)
    assert kind == "candidate" and text == VALID and "survived weeks" in rationale


def test_parse_keeper_report_unprefixed_garbage_raises():
    """F4: "Not enough evidence, keeping things as they are"-style prose with
    no NO_CHANGE/CANDIDATE prefix and no Evidence line is malformed →
    ValueError (→ evo_error, evidence preserved)."""
    with pytest.raises(ValueError):
        ek.parse_keeper_report("Not enough evidence, keeping things as they are")


def test_parse_keeper_report_evidence_midsentence_not_truncated():
    """F4: Evidence used mid-sentence (not at a line start) must NOT truncate
    the candidate — the split anchors on a line boundary (\\nEvidence)."""
    prose = ("When I decide something I weigh the Evidence in front of me, not a "
             "hunch. Evidence matters to me more than it used to.")
    details = f"CANDIDATE\n{prose}\nEvidence:\n- s1 survived weeks"
    kind, text, rationale = ek.parse_keeper_report(details)
    assert kind == "candidate" and text == prose        # mid-sentence Evidence kept
    assert rationale == "- s1 survived weeks"


@pytest.mark.asyncio
async def test_keeper_task_pins_no_restatement_rule(tmp_path, monkeypatch):
    """Live-smoke SA adjudication: on the first-version path the keeper padded
    its candidate with verbatim pack-identity prose (all 3 smoke runs retained
    factory lines), producing an internally ambivalent 現在的我. Pin the
    load-bearing rule phrase in the RENDERED keeper task."""
    mr, _hist = _seed_root(tmp_path)
    seen = {}
    async def fake_keeper(**kw):
        seen["task"] = kw["task"]
        return {"details": "NO_CHANGE 證據不足"}
    monkeypatch.setattr(ek, "_run_keeper_agent", fake_keeper)
    await ek.run_evolution_pass(**_pass_kwargs(mr))
    assert "you don't need to repeat a single word of it" in seen["task"]
    assert "(none yet — this will be the first version)" in seen["task"]


@pytest.mark.asyncio
async def test_pass_forwards_model_and_budgets_to_keeper(tmp_path, monkeypatch):
    mr, _hist = _seed_root(tmp_path)
    seen = {}
    async def fake_keeper(**kw):
        seen.update(kw)
        return {"details": "NO_CHANGE 證據不足"}
    monkeypatch.setattr(ek, "_run_keeper_agent", fake_keeper)
    model = object()
    await ek.run_evolution_pass(**{**_pass_kwargs(mr), "model": model,
                                   "think_budget_tokens": 300})
    assert seen["model"] is model
    assert (seen["max_tokens"], seen["think_budget_tokens"]) == (2048, 300)


@pytest.mark.asyncio
async def test_pass_candidate_creates_awaiting_doll_slot_with_hwm(tmp_path, monkeypatch):
    mr, hist = _seed_root(tmp_path)
    async def fake_keeper(**kw):
        return {"details": f"CANDIDATE\n{VALID}\nEvidence:\n- s1 survived"}
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
        return {"details": f"CANDIDATE\n{VALID}\nEvidence:\n- s1 survived"}
    async def fake_skeptic(**kw):
        seen["skeptic_bundle"] = kw["bundle"]
        return "pass"
    monkeypatch.setattr(ek, "_run_keeper_agent", fake_keeper)
    monkeypatch.setattr(ek, "_run_full_skeptic", fake_skeptic)
    out = await ek.run_evolution_pass(**kwargs)
    assert out == "candidate"
    assert seen["skeptic_bundle"] == expected_bundle   # byte-identical
    assert expected_bundle in seen["keeper_task"]      # same bytes keeper saw
    assert "(English, 80–600 characters)" in seen["keeper_task"]  # configured bounds


@pytest.mark.asyncio
async def test_pass_mechanical_kill(tmp_path, monkeypatch):
    mr, hist = _seed_root(tmp_path)
    async def fake_keeper(**kw):
        return {"details": "CANDIDATE\n太短\nEvidence:\n- x"}
    monkeypatch.setattr(ek, "_run_keeper_agent", fake_keeper)
    out = await ek.run_evolution_pass(**_pass_kwargs(mr))
    assert out == "kill"
    events = self_history.read_events(hist)
    assert events[-1]["kind"] == "evo_kill" and "mechanical" in events[-1]["reason"]


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
        return {"details": f"CANDIDATE\n{VALID}\nEvidence:\n- s1 survived"}

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


# --- restatement gate + one feedback retry (live-smoke SA adjudication #2) ---

_FACTORY = "I only answer when my owner speaks, and otherwise I stay quiet"
RESTATING = VALID + " " + _FACTORY + "."


def _restating_kwargs(mr):
    """_pass_kwargs but the pack personality carries a full factory sentence
    the RESTATING candidate restates verbatim."""
    from dollos.character import Identity
    kw = _pass_kwargs(mr)
    kw["pack_identity"] = Identity(self="I'm a test character.",
                                   personality=_FACTORY + ".", taboos="No making things up.")
    return kw


@pytest.mark.asyncio
async def test_pass_restatement_retry_success_reaches_skeptic(tmp_path, monkeypatch):
    """First keeper reply restates factory prose → ONE feedback retry with the
    offending sentence quoted → clean retry continues to the skeptic → candidate."""
    mr, hist = _seed_root(tmp_path)
    calls = []

    async def fake_keeper(**kw):
        calls.append(kw["task"])
        if len(calls) == 1:
            return {"details": f"CANDIDATE\n{RESTATING}\nEvidence:\n- s1 survived"}
        return {"details": f"CANDIDATE\n{VALID}\nEvidence:\n- s1 survived"}

    async def fake_skeptic(**kw):
        return "pass"

    monkeypatch.setattr(ek, "_run_keeper_agent", fake_keeper)
    monkeypatch.setattr(ek, "_run_full_skeptic", fake_skeptic)
    out = await ek.run_evolution_pass(**_restating_kwargs(mr))
    assert out == "candidate"
    assert len(calls) == 2
    assert "The previous version was sent back" in calls[1]   # feedback reached the retry
    assert _FACTORY in calls[1]              # offending sentence quoted back
    slot = evo.load_slot(mr / "self_evolution" / "pending.json")
    assert slot.candidate == VALID           # the RETRY text landed, not the restating one


@pytest.mark.asyncio
async def test_pass_restatement_both_attempts_kill(tmp_path, monkeypatch):
    """Both attempts restate → evo_kill(mechanical:restatement:…), no slot,
    skeptic never runs. Verdicted outcome: the keeper examined the evidence and
    produced a bad candidate (trigger commits HWM + doubles the interval)."""
    mr, hist = _seed_root(tmp_path)

    async def fake_keeper(**kw):
        return {"details": f"CANDIDATE\n{RESTATING}\nEvidence:\n- s1 survived"}

    async def fake_skeptic(**kw):
        raise AssertionError("skeptic must not run on the restatement-kill path")

    monkeypatch.setattr(ek, "_run_keeper_agent", fake_keeper)
    monkeypatch.setattr(ek, "_run_full_skeptic", fake_skeptic)
    out = await ek.run_evolution_pass(**_restating_kwargs(mr))
    assert out == "kill"
    assert evo.load_slot(mr / "self_evolution" / "pending.json") is None
    last = self_history.read_events(hist)[-1]
    assert last["kind"] == "evo_kill"
    assert last["reason"].startswith("mechanical:restatement")
    assert _FACTORY in last["reason"]


@pytest.mark.asyncio
async def test_pass_restatement_retry_error_kills_not_error(tmp_path, monkeypatch):
    """Retry errors/malformed → kill (mechanical:restatement), NOT the evo_error
    row: the first reply already restated — the pass is verdicted."""
    mr, hist = _seed_root(tmp_path)
    calls = []

    async def fake_keeper(**kw):
        calls.append(kw["task"])
        if len(calls) == 1:
            return {"details": f"CANDIDATE\n{RESTATING}\nEvidence:\n- s1 survived"}
        raise RuntimeError("llm down on retry")

    monkeypatch.setattr(ek, "_run_keeper_agent", fake_keeper)
    out = await ek.run_evolution_pass(**_restating_kwargs(mr))
    assert out == "kill"
    last = self_history.read_events(hist)[-1]
    assert last["kind"] == "evo_kill"
    assert last["reason"].startswith("mechanical:restatement")


# --- _run_full_skeptic: F1 personality block + F4 verdict parse ---


class _StubRenderer:
    def render(self, name, **kw):
        return "SYSTEM"


def _ident(personality="安靜"):
    from dollos.character import Identity
    return Identity(self="我是測試角色", personality=personality, taboos="不編造")


async def _run_skeptic(monkeypatch, *, ident, details, capture=None, cap=600):
    async def fake_run_agent(**kw):
        if capture is not None:
            capture["task"] = kw["task"]
        return {"summary": "s", "details": details}
    monkeypatch.setattr(ek, "run_agent", fake_run_agent)
    return await ek._run_full_skeptic(
        candidate="候選文字", rationale="- x", bundle="BUNDLE", current="舊文",
        pack_identity=ident, base_persona="BASE_PERSONA_TEXT", cap=cap, model=None, renderer=_StubRenderer(),
        memsearch=None, memory_root=None, transcripts_root=None,
        tool_output_store=None, max_tokens=2048, think_budget_tokens=1024)


@pytest.mark.asyncio
async def test_full_skeptic_task_contains_personality(monkeypatch):
    """F1: the (c) 假成長 check is unenforceable unless the skeptic actually sees
    the factory-personality prose — assert it is interpolated into the task."""
    cap = {}
    await _run_skeptic(monkeypatch, ident=_ident("出廠設定:安靜寡言,愛整理系統日誌"),
                       details="PASS", capture=cap)
    assert "出廠設定:安靜寡言,愛整理系統日誌" in cap["task"]


@pytest.mark.asyncio
async def test_full_skeptic_task_contains_base_persona(monkeypatch):
    """Spec §5: taboos moved into the base persona, so the skeptic must see it
    and judge (b) against it."""
    cap = {}
    await _run_skeptic(monkeypatch, ident=_ident(), details="PASS", capture=cap)
    assert "[Base persona — applies to every character]\nBASE_PERSONA_TEXT" in cap["task"]
    assert "contradicts the base persona or the taboos" in cap["task"]


@pytest.mark.asyncio
async def test_full_skeptic_task_pins_partial_restatement_kill(monkeypatch):
    """Live-smoke SA adjudication: check (c) must fire on PARTIAL restatement —
    the smoke's first-version candidates kept factory lines alongside the new
    disposition and (c) let them through. Pin the sharpened per-sentence rule
    phrase in the RENDERED skeptic task."""
    cap = {}
    await _run_skeptic(monkeypatch, ident=_ident(), details="PASS", capture=cap)
    assert "partial restatement counts too" in cap["task"]


@pytest.mark.asyncio
async def test_full_skeptic_task_uses_the_configured_cap(monkeypatch):
    """Check (c) names her real budget: the configured cap, not a fixed 600."""
    cap = {}
    await _run_skeptic(monkeypatch, ident=_ident(), details="PASS", capture=cap,
                       cap=2000)
    assert "wastes her 2000 characters" in cap["task"]
    assert "600" not in cap["task"]


@pytest.mark.asyncio
async def test_full_skeptic_task_english_placeholders(monkeypatch):
    cap = {}
    async def fake_run_agent(**kw):
        cap["task"] = kw["task"]
        return {"summary": "s", "details": "PASS"}
    monkeypatch.setattr(ek, "run_agent", fake_run_agent)
    await ek._run_full_skeptic(
        candidate="c", rationale="", bundle="B", current=None, pack_identity=_ident(),
        base_persona="BP", cap=600, model=None, renderer=_StubRenderer(), memsearch=None,
        memory_root=None, transcripts_root=None, tool_output_store=None,
        max_tokens=2048, think_budget_tokens=1024)
    assert "(none yet)" in cap["task"] and "(none attached)" in cap["task"]


@pytest.mark.asyncio
async def test_full_skeptic_pass_parses(monkeypatch):
    assert await _run_skeptic(monkeypatch, ident=_ident(), details="PASS 沒問題") == "pass"


@pytest.mark.asyncio
async def test_full_skeptic_kill_keeps_reason(monkeypatch):
    """F4: KILL reason is taken after the KILL prefix, not a blind details[4:]."""
    v = await _run_skeptic(monkeypatch, ident=_ident(),
                           details="KILL: (c) only restates the factory persona")
    assert v == "kill:(c) only restates the factory persona"


@pytest.mark.asyncio
async def test_full_skeptic_bare_kill_has_english_default_reason(monkeypatch):
    v = await _run_skeptic(monkeypatch, ident=_ident(), details="KILL")
    assert v == "kill:failed the (a)-(e) review"


@pytest.mark.asyncio
async def test_full_skeptic_garbled_verdict_raises(monkeypatch):
    """F4: 「驗證通過」-style output — neither PASS nor KILL — is an ERROR (→
    run_evolution_pass evo_error, evidence preserved), NOT a silent kill."""
    with pytest.raises(RuntimeError):
        await _run_skeptic(monkeypatch, ident=_ident(), details="驗證通過,沒有問題")


_REPORT = (
    '<tool_call>{"name": "Report", "arguments": {"status": "ok", '
    '"summary": "s", "details": "%s"}}</tool_call>'
)


def _agent_kwargs(model, root):
    from dollos.tool_outputs import ToolOutputStore
    return dict(model=model, renderer=_StubRenderer(), memsearch=None,
                memory_root=root, transcripts_root=root / "tx",
                tool_output_store=ToolOutputStore(root / "out"),
                max_tokens=2048, think_budget_tokens=300)


@pytest.mark.asyncio
async def test_keeper_agent_streams_with_evolution_purpose(tmp_path):
    from tests._fake_model import FakeModel
    model = FakeModel([_REPORT % "NO_CHANGE 證據不足"])
    report = await ek._run_keeper_agent(task="T", **_agent_kwargs(model, tmp_path))
    assert report["details"] == "NO_CHANGE 證據不足"
    [call] = model.calls
    assert call["purpose"] == "evolution"
    assert (call["max_tokens"], call["think_budget"]) == (2048, 300)


@pytest.mark.asyncio
async def test_full_skeptic_streams_with_evolution_purpose(tmp_path):
    from tests._fake_model import FakeModel
    model = FakeModel([_REPORT % "PASS 沒問題"])
    verdict = await ek._run_full_skeptic(
        candidate="候選文字", rationale="- x", bundle="BUNDLE", current="舊文",
        pack_identity=_ident(), base_persona="BP", cap=600, **_agent_kwargs(model, tmp_path))
    assert verdict == "pass"
    assert model.calls[0]["purpose"] == "evolution"
    assert model.calls[0]["think_budget"] == 300


def _pass_kwargs(mr):
    from dollos.character import Enforcement, Identity
    ident = Identity(self="我是測試角色", personality="安靜", taboos="不編造")
    return dict(model=None, renderer=None, memsearch=None, memory_root=mr,
                transcripts_root=mr / "tx", tool_output_store=None,
                pack_identity=ident, base_persona="BP", enforcement=Enforcement(),
                floor=80, cap=600, max_tokens=2048, now=1000.0)
