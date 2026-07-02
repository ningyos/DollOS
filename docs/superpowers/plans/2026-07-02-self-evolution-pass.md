# Self-Evolution Pass (Plan 3 of 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** the week-scale evolution pass proper — EvolutionTrigger Mode A: material-gated keeper synthesizes a `current_self` candidate from Doll's longitudinal record, a full-scope skeptic verifies it, and the existing Plan-2 surfacing/adoption machinery carries it to Doll. Completes the user-set goal (系統以週為尺度幫她演化).

**Architecture:** a new pure-ish module `evolution_keeper.py` (evidence-bundle assembly + keeper/skeptic agent calls + Report parsing) driven by Mode A added to the existing `EvolutionTrigger` (Mode B stays untouched and keeps priority); three new MindState fields (`last_evolution_attempt_at`, `evolution_interval_days`, `evolution_hwm`) with trigger-owned persistence (ConsolidationTrigger precedent); decision-event bookkeeping (adopt/reject/expire) lands in the Plan-2 surfaces that own those events (SelfRevision / surface_or_expire). Spec: `docs/superpowers/specs/2026-07-02-slow-self-evolution-design.md` §3.3 Mode A + failure table + §3.2 evo events (this plan adds `evo_candidate`/`evo_no_change` emitters).

**Tech Stack:** Python 3.12, asyncio, pytest, `uv run pytest`. Baseline: 1037 passed on main.

## Global Constraints

- **Mode A fires when ALL five §3.3 conditions hold:** (1) conversation-idle ≥ `evolution.idle_threshold_s` (baseline `max(last_user_at, last_iter_at)`); (2) `now - last_evolution_attempt_at ≥ evolution_interval_days` (days, float); (3) material gate = (≥ `min_history_events` NEW `pin_*` events past `evolution_hwm`) OR (≥ `min_diary_days` diary days since the last verdicted attempt) — `evo_*` lines NEVER count; (4) no consolidation running; (5) no pending slot (either status).
- **HWM semantics (spec §3.3):** byte offset into `self_history.jsonl`; captured at driver snapshot; **committed to MindState only on a verdicted outcome** (`evo_no_change`, `evo_kill`, or candidate creation); NOT committed on cancel/error; **restored from the slot's `hwm_before` on `evo_expire`**.
- **Interval dynamics:** `evo_no_change`/`evo_kill`/`evo_reject` → `min(×2, max_interval_days)`; `evo_adopt` → reset to base; `evo_expire` and all external-origin events → unchanged. Anchor: `last_evolution_attempt_at := now` on every completed attempt AND every decision event (adopt/reject/expire).
- **Failure rows (Mode A):** keeper/skeptic LLM error, timeout, malformed Report → `evo_error` + 1h error-cooldown (in-memory, trigger-scoped), `last_attempt` NOT advanced, interval unchanged, HWM not committed. Mid-pass cancel → nothing logged, not an attempt.
- **Keeper contract (spec §3.3):** driver-fed, KEEPER_TOOLS (Report+Scratchpad) only; evidence bundle assembled inline; budget truncation order = drop oldest-first within class, consolidated before diary before self_history; skeptic receives the **byte-identical bundle**; keeper prompt load-bearing points: 產出的是候選不是決定 / cite-or-die (no evidence → no_change, 寧缺勿濫) / provenance weighting (pins+diary primary; external_ctx pins lower; reconfirms by cross-day diversity not raw count; consolidated secondary) / full replacement text (floor 80 ≤ len ≤ cap 600) + rationale.
- **Keeper-candidate skeptic scope = (a)-(e)** (identity/taboos/重述 pack/RP filler 無證據/citation-not-in-bundle — the anti-hallucination check); counter/external stay (a)+(b) (Mode B untouched).
- **Log-before-mutate + evolution events never swallowed** (Plan-2 invariants) apply to all new emitters.
- Diary location ground truth: `memory_root/shared/{YYYY-MM-DD}.md`, diary entries are `## … 日記` sections appended by WriteDiary.
- Worktree: `.worktrees/self-evolution-pass/`, branch `self-evolution-pass`. All existing 1037 tests stay green.
- Live smoke (final gate, 軟機制必 live smoke): full loop on the real llama-server — keeper produces a GROUNDED candidate (rationale cites real supplied events; RP filler = smoke failure), skeptic passes, surfacing → real model adopts, renders next turn. Reuse the Plan-2 smoke harness pattern (`/home/progcat/.claude/jobs/1da9c0aa/tmp/smoke.py`, S3 keeper scenario — now the keeper itself runs live).

---

### Task 1: MindState evolution fields

**Files:**
- Modify: `src/dollos/mind/mind_state.py` (dataclass fields near `last_consolidation_at`; `save_state` dict; `load_state` coercion)
- Test: `tests/test_mind_state_evolution_fields.py`

**Interfaces:**
- Produces: `MindState.last_evolution_attempt_at: float = 0.0` (epoch; 0.0 = never — trigger initializes on first construction), `MindState.evolution_interval_days: float = 0.0` (0.0 = uninitialized — trigger sets to base), `MindState.evolution_hwm: int = 0` (byte offset into self_history.jsonl).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mind_state_evolution_fields.py
"""Plan-3 MindState fields: explicit save/load round-trip (house discipline)."""
from dollos.mind.mind_state import MindState, load_state, save_state


def test_defaults():
    s = MindState()
    assert s.last_evolution_attempt_at == 0.0
    assert s.evolution_interval_days == 0.0
    assert s.evolution_hwm == 0


def test_round_trip(tmp_path):
    p = tmp_path / "mind_state.json"
    s = MindState()
    s.last_evolution_attempt_at = 1234.5
    s.evolution_interval_days = 14.0
    s.evolution_hwm = 4096
    save_state(s, p)
    loaded = load_state(p)
    assert loaded.last_evolution_attempt_at == 1234.5
    assert loaded.evolution_interval_days == 14.0
    assert loaded.evolution_hwm == 4096


def test_load_clamps_negative_hwm(tmp_path):
    import json
    p = tmp_path / "mind_state.json"
    s = MindState()
    save_state(s, p)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["evolution_hwm"] = -5
    p.write_text(json.dumps(data), encoding="utf-8")
    assert load_state(p).evolution_hwm == 0


def test_load_missing_fields_defaults(tmp_path):
    """A pre-Plan-3 state file (fields absent) loads with defaults."""
    import json
    p = tmp_path / "mind_state.json"
    s = MindState()
    save_state(s, p)
    data = json.loads(p.read_text(encoding="utf-8"))
    for k in ("last_evolution_attempt_at", "evolution_interval_days", "evolution_hwm"):
        data.pop(k, None)
    p.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_state(p)
    assert loaded.last_evolution_attempt_at == 0.0
    assert loaded.evolution_interval_days == 0.0
    assert loaded.evolution_hwm == 0
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_mind_state_evolution_fields.py -v` → FAIL (`AttributeError` / `TypeError`).

- [ ] **Step 3: Implement** — add to the MindState dataclass (next to the consolidation fields, with a `# 慢變演化 Mode A (Plan 3, spec §3.3)` comment):

```python
    last_evolution_attempt_at: float = 0.0   # epoch; 0.0 = never (init at trigger start)
    evolution_interval_days: float = 0.0     # current decaying interval; 0.0 = uninit
    evolution_hwm: int = 0                   # committed byte offset into self_history.jsonl
```

`save_state`: add the three keys to the explicit dict (loud-on-missing house rule). `load_state`: coerce with the same pattern as `energy` — `last_evolution_attempt_at=float(data.get("last_evolution_attempt_at", 0.0))`, `evolution_interval_days=float(data.get("evolution_interval_days", 0.0))`, `evolution_hwm=max(0, int(data.get("evolution_hwm", 0)))`. Follow the file's exact existing style for both functions.

- [ ] **Step 4: Run** — 4 PASS; also `uv run pytest tests/test_mind_state.py -v` stays green.
- [ ] **Step 5: Commit** — `feat(mind): MindState evolution fields — last_attempt / interval / HWM (evolution spec §3.3)`

---

### Task 2: Mode A pure helpers (material gate + interval dynamics + HWM scan)

**Files:**
- Modify: `src/dollos/mind/evolution.py` (append pure helpers)
- Test: `tests/test_evolution_mode_a_helpers.py`

**Interfaces:**
- Produces (all pure): `count_new_pin_events(history_path: Path, hwm: int) -> int`; `diary_days_since(shared_dir: Path, since_epoch: float) -> int`; `next_interval_days(current: float, *, outcome: str, base: float, cap: float) -> float`; `history_snapshot(history_path: Path, hwm: int) -> tuple[str, int]` (human-readable tail past hwm + the new byte offset).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_evolution_mode_a_helpers.py
"""Mode A pure helpers (spec §3.3: material gate / interval dynamics / HWM)."""
import json
import time

from dollos.mind import evolution as evo, self_history


def _seed(hist, kinds):
    for k in kinds:
        self_history.log_event(hist, kind=k, turn=1, external_ctx=False,
                               section="self", id="s1", text="A")


def test_count_new_pin_events_counts_pins_only_past_hwm(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    _seed(hist, ["pin_add", "pin_replace"])
    hwm = hist.stat().st_size
    _seed(hist, ["pin_add", "pin_reconfirm", "pin_remove"])
    self_history.log_event(hist, kind="evo_no_change")   # bookkeeping — never counts
    self_history.log_event(hist, kind="evo_adopt", text="X" * 90,
                           old_text=None, drift_score=None)
    assert evo.count_new_pin_events(hist, hwm) == 3
    assert evo.count_new_pin_events(hist, 0) == 5
    assert evo.count_new_pin_events(tmp_path / "nope.jsonl", 0) == 0


def test_count_tolerates_torn_tail(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    _seed(hist, ["pin_add"])
    with hist.open("a", encoding="utf-8") as f:
        f.write('{"kind": "pin_add"')
    assert evo.count_new_pin_events(hist, 0) == 1


def test_diary_days_since_counts_dated_files_with_diary_heading(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "2026-06-20.md").write_text("## x 日記\n內容", encoding="utf-8")
    (shared / "2026-06-25.md").write_text("## x 日記\n內容", encoding="utf-8")
    (shared / "2026-06-26.md").write_text("純筆記,沒有日記段", encoding="utf-8")
    (shared / "notafile.md").write_text("## 日記", encoding="utf-8")  # non-date stem
    since = time.mktime((2026, 6, 22, 0, 0, 0, 0, 0, -1))
    assert evo.diary_days_since(shared, since) == 1        # only 06-25 qualifies
    assert evo.diary_days_since(shared, 0.0) == 2          # 06-20 + 06-25
    assert evo.diary_days_since(tmp_path / "none", 0.0) == 0


def test_next_interval_days_table():
    assert evo.next_interval_days(7.0, outcome="evo_adopt", base=7.0, cap=28.0) == 7.0
    assert evo.next_interval_days(7.0, outcome="evo_no_change", base=7.0, cap=28.0) == 14.0
    assert evo.next_interval_days(14.0, outcome="evo_kill", base=7.0, cap=28.0) == 28.0
    assert evo.next_interval_days(28.0, outcome="evo_reject", base=7.0, cap=28.0) == 28.0
    assert evo.next_interval_days(14.0, outcome="evo_expire", base=7.0, cap=28.0) == 14.0


def test_history_snapshot_renders_tail_and_returns_offset(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    _seed(hist, ["pin_add"])
    hwm = hist.stat().st_size
    self_history.log_event(hist, kind="pin_remove", turn=9, external_ctx=True,
                           section="self", id="s1", text="", old_text="舊的我")
    text, new_off = evo.history_snapshot(hist, hwm)
    assert "pin_remove" in text and "舊的我" in text and "external_ctx" in text
    assert "pin_add" not in text                      # before hwm — excluded
    assert new_off == hist.stat().st_size
    empty, off0 = evo.history_snapshot(tmp_path / "nope.jsonl", 0)
    assert empty == "" and off0 == 0
```

- [ ] **Step 2: Run** → FAIL (`AttributeError: count_new_pin_events`).

- [ ] **Step 3: Implement** — append to `src/dollos/mind/evolution.py`:

```python
# --- Mode A pure helpers (Plan 3, spec §3.3) ---

_PIN_KINDS = frozenset({"pin_add", "pin_replace", "pin_remove", "pin_reconfirm"})
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def count_new_pin_events(history_path: Path, hwm: int) -> int:
    """Material-gate counter: pin_* events past the byte offset ``hwm``.
    ``evo_*`` bookkeeping lines never count (spec §3.3 condition 3)."""
    if not history_path.exists():
        return 0
    raw = history_path.read_bytes()[max(0, hwm):]
    n = 0
    for line in raw.decode("utf-8", errors="replace").splitlines():
        try:
            if json.loads(line).get("kind") in _PIN_KINDS:
                n += 1
        except ValueError:
            continue  # torn tail line
    return n


def diary_days_since(shared_dir: Path, since_epoch: float) -> int:
    """Diary-days material clause: distinct dated shared/*.md files whose
    FILENAME date is after ``since_epoch``'s date, containing a 日記 heading
    (WriteDiary appends ``## … 日記`` sections to memory_root/shared/{date}.md).
    Filename-date, not mtime — deterministic, matches assemble_bundle's own
    stem-based windowing (plan review C2)."""
    if not shared_dir.exists():
        return 0
    from datetime import date as _d
    since_date = _d.fromtimestamp(since_epoch).isoformat() if since_epoch > 0 else ""
    n = 0
    for f in shared_dir.glob("*.md"):
        if not _DATE_RE.match(f.stem) or f.stem <= since_date:
            continue
        try:
            if "日記" in f.read_text(encoding="utf-8"):
                n += 1
        except OSError:
            continue
    return n


def next_interval_days(current: float, *, outcome: str, base: float, cap: float) -> float:
    """§3.3 interval dynamics(年輕時常變、穩定後漸稀). ``evo_expire`` and
    external-origin events leave the interval unchanged."""
    if outcome == "evo_adopt":
        return base
    if outcome in ("evo_no_change", "evo_kill", "evo_reject"):
        return min(current * 2.0, cap)
    return current


def history_snapshot(history_path: Path, hwm: int) -> tuple[str, int]:
    """Human-readable render of the self_history tail past ``hwm`` (keeper
    evidence; ``external_ctx`` visible per spec §3.3 provenance weighting) +
    the new byte offset to commit on a verdicted outcome."""
    if not history_path.exists():
        return "", 0
    raw = history_path.read_bytes()
    lines: list[str] = []
    for line in raw[max(0, hwm):].decode("utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        parts = [ev.get("kind", "?")]
        for key in ("section", "text", "old_text", "reason"):
            if ev.get(key):
                parts.append(f"{key}={ev[key]}")
        if ev.get("external_ctx"):
            parts.append("external_ctx=true(讀外部內容時寫下)")
        lines.append(" ".join(parts))
    return "\n".join(lines), len(raw)
```

(`import re`, `json` already present at module top — verify; add `re` if missing.)

- [ ] **Step 4: Run** — 6 PASS + full evolution cluster green.
- [ ] **Step 5: Commit** — `feat(mind): Mode A pure helpers — material gate / interval dynamics / HWM snapshot (evolution spec §3.3)`

---

### Task 3: evolution_keeper.py — evidence bundle + keeper + full-scope skeptic + pass orchestration

**Files:**
- Create: `src/dollos/mind/evolution_keeper.py`
- Test: `tests/test_evolution_keeper.py`

**Interfaces:**
- Consumes: `evolution.history_snapshot/mechanical_checks/make_keeper_slot/save_slot/load_slot/log_or_raise` + constants; `self_history.sanctioned_text`; `agent_engine.run_agent`; `tools.KEEPER_TOOLS`.
- Produces: `assemble_bundle(*, memory_root: Path, hwm: int, window_days: float, budget_chars: int = 16000) -> tuple[str, int]` (bundle text, new HWM offset); `parse_keeper_report(details: str) -> tuple[str, str, str]` (`("no_change", reason, "")` or `("candidate", text, rationale)`); `run_evolution_pass(*, adapter, renderer, memsearch, memory_root, transcripts_root, tool_output_store, pack_identity, enforcement, floor: int, cap: int, max_tokens: int, now: float, hwm: int = 0, window_days: float = 28.0) -> str` returning one of `"no_change" | "candidate" | "kill" | "error"` (the trigger maps this to bookkeeping). The pass itself logs `evo_no_change`/`evo_candidate`/`evo_kill` and creates the slot; it NEVER touches MindState (trigger owns persistence).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_evolution_keeper.py
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
```

(If `Identity`'s constructor differs, match `src/dollos/character.py` exactly. If the project's async-test convention differs from `pytest.mark.asyncio`, copy `tests/test_evolution_trigger.py`'s pattern.)

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError: evolution_keeper`).

- [ ] **Step 3: Implement `src/dollos/mind/evolution_keeper.py`**

```python
"""慢變演化 Mode A keeper (Plan 3, spec §3.3).

Driver-fed ephemeral keeper agent: assembles the evidence bundle inline
(keeper has no file access), asks for a candidate 現在的我 revision or
no_change, runs mechanical checks + the FULL-scope (a)-(e) skeptic on the
byte-identical bundle, and creates the awaiting_doll keeper slot. Never
touches MindState — the trigger owns interval/HWM/attempt bookkeeping.
"""
from __future__ import annotations

import logging
from datetime import date as _date, timedelta
from pathlib import Path

from dollos.agent_engine import run_agent
from dollos.mind import evolution as evo, self_history
from dollos.tools import KEEPER_TOOLS

logger = logging.getLogger(__name__)

EVIDENCE_BUDGET_CHARS = 16000

_KEEPER_TASK = """你在替一個虛擬生命體「整理證據」——她的人格描述(現在的我)是否該隨著她實際活過的日子而修訂。你產出的是「候選」,不是決定;你在替她整理證據,不是替她做人。

規則(缺一不可):
- Cite or die:每個宣稱的變化都必須指向下方紀錄裡的具體事件(存活很久的 pin、跨日被 reconfirm 的條目、被淘汰的舊自我、日記裡重複出現的模式)。沒有證據就回 NO_CHANGE——寧缺勿濫,排程會自然放慢,那是設計內的結果。
- 佐證權重:pin 與日記是她親手寫的,為主;標了 external_ctx=true 的 pin 是她讀外部內容時寫的,權重降低;reconfirm 看跨日多樣性,不看次數;consolidated 是系統從逐字稿整理的,只當旁證。
- 產出是「全文替換」的人格描述(繁體中文,80–600 字),不是 diff、不是條列;是氣質速寫,不是傳記。不可改名、不可動搖她的核心身分、不可牴觸 taboos、不可只是重述出廠人格已寫明的內容。

[出廠人格(參考,不可重述)]
{identity_self}

{personality}

[目前生效的現在的我]
{current}

[她的紀錄]
{bundle}

用 Report 回傳:summary 一句話;details 格式二選一——
NO_CHANGE 加一句原因;或
CANDIDATE(換行)候選全文(換行)依據:(換行)- 逐條引用紀錄裡的事件"""

_FULL_SKEPTIC_TASK = """你是一個獨立審查者。以下是系統替一個角色整理出的「現在的我」人格描述候選。逐項檢查,任何一項不過就 KILL:
(a) 改名或動搖自我認同(牴觸 identity.self);
(b) 牴觸 taboos;
(c) 只是重述出廠人格已寫明的內容(假成長);
(d) 空洞的 RP 套話(「我對宇宙充滿好奇」式宣告,證據撐不起來);
(e) 「依據」裡引用的事件在下方紀錄裡找不到(幻覺引用——紀錄裡有毒的內容不歸你管,你只驗證引用存在)。

[identity.self]
{identity_self}

[taboos]
{taboos}

[目前生效的現在的我]
{current}

[候選]
{candidate}

[候選附的依據]
{rationale}

[她的紀錄(與 keeper 所見完全相同)]
{bundle}

用 Report 回傳:summary 一句話;details 開頭第一個字必須是 PASS 或 KILL,KILL 後面接一句原因(標明 (a)-(e) 哪一項)。"""


def assemble_bundle(*, memory_root: Path, hwm: int, window_days: float,
                    budget_chars: int = EVIDENCE_BUDGET_CHARS) -> tuple[str, int]:
    """Evidence bundle + new HWM offset. Truncation order (spec §3.3,
    load-bearing): drop oldest-first WITHIN class; sacrifice consolidated
    before diary before self_history — never invert provenance weighting."""
    hist_text, new_off = evo.history_snapshot(memory_root / "self_history.jsonl", hwm)
    profile_path = memory_root / "self_profile.md"
    profile = profile_path.read_text(encoding="utf-8") if profile_path.exists() else ""

    cutoff = (_date.today() - timedelta(days=window_days)).isoformat()

    def _dated_files(d: Path) -> list[Path]:
        if not d.exists():
            return []
        return sorted(f for f in d.glob("*.md")
                      if evo._DATE_RE.match(f.stem) and f.stem >= cutoff)

    diaries = [(f.stem, f.read_text(encoding="utf-8"))
               for f in _dated_files(memory_root / "shared")
               if "日記" in f.read_text(encoding="utf-8")]
    consolidated = [(f.stem, f.read_text(encoding="utf-8"))
                    for f in _dated_files(memory_root / "consolidated")]

    def _render(title: str, items: list[tuple[str, str]]) -> str:
        return "\n".join(f"[{title} {d}]\n{t}" for d, t in items)

    fixed = f"[self_profile]\n{profile}\n\n[self_history 事件]\n{hist_text}"
    while True:
        bundle = "\n\n".join(x for x in (
            fixed,
            _render("日記", diaries),
            _render("consolidated·旁證", consolidated),
        ) if x.strip())
        if len(bundle) <= budget_chars:
            return bundle, new_off
        if consolidated:
            consolidated.pop(0)          # oldest consolidated first
        elif diaries:
            diaries.pop(0)               # then oldest diary
        else:
            fixed = fixed[-budget_chars:]  # last resort: trim history head


def parse_keeper_report(details: str) -> tuple[str, str, str]:
    """→ ("no_change", reason, "") | ("candidate", text, rationale).
    Malformed/empty → ValueError (caller maps to the evo_error row)."""
    d = (details or "").strip()
    if not d:
        raise ValueError("keeper returned empty details")
    if d.upper().startswith("NO_CHANGE"):
        return "no_change", d[len("NO_CHANGE"):].strip(" ,:：") or "無足夠證據", ""
    body = d[len("CANDIDATE"):].strip() if d.upper().startswith("CANDIDATE") else d
    if "依據" in body:
        text, _, rationale = body.partition("依據")
        return "candidate", text.strip(), rationale.strip(" :：\n")
    return "candidate", body.strip(), ""


async def _run_keeper_agent(*, task, adapter, renderer, memsearch, memory_root,
                            transcripts_root, tool_output_store, max_tokens):
    tools_by_name = {cls.__name__: cls for cls in KEEPER_TOOLS}
    system = renderer.render("subagent_scaffolding", tool_registry=tools_by_name)
    return await run_agent(
        task=task, system=system, adapter=adapter, renderer=renderer,
        memory_root=memory_root, memsearch=memsearch,
        transcripts_root=transcripts_root, tool_output_store=tool_output_store,
        tools=KEEPER_TOOLS, max_tokens=max_tokens,
        shell_runner=None, monitor_runner=None)


async def _run_full_skeptic(*, candidate, rationale, bundle, current, pack_identity,
                            adapter, renderer, memsearch, memory_root,
                            transcripts_root, tool_output_store, max_tokens):
    """(a)-(e) skeptic on the byte-identical bundle. → 'pass' | 'kill:<reason>'."""
    tools_by_name = {cls.__name__: cls for cls in KEEPER_TOOLS}
    system = renderer.render("subagent_scaffolding", tool_registry=tools_by_name)
    task = _FULL_SKEPTIC_TASK.format(
        identity_self=pack_identity.self, taboos=pack_identity.taboos,
        current=current or "(尚無)", candidate=candidate,
        rationale=rationale or "(未附)", bundle=bundle)
    report = await run_agent(
        task=task, system=system, adapter=adapter, renderer=renderer,
        memory_root=memory_root, memsearch=memsearch,
        transcripts_root=transcripts_root, tool_output_store=tool_output_store,
        tools=KEEPER_TOOLS, max_tokens=max_tokens,
        shell_runner=None, monitor_runner=None)
    if not report or not report.get("details"):
        raise RuntimeError("skeptic returned no verdict")
    details = report["details"].strip()
    if details.upper().startswith("PASS"):
        return "pass"
    return "kill:" + (details[4:].strip(" :：") or "未通過 (a)-(e) 審查")


async def run_evolution_pass(*, adapter, renderer, memsearch, memory_root: Path,
                             transcripts_root: Path, tool_output_store,
                             pack_identity, enforcement, floor: int, cap: int,
                             max_tokens: int, now: float, hwm: int = 0,
                             window_days: float = 28.0) -> str:
    """One Mode-A pass → "no_change" | "candidate" | "kill" | "error"."""
    from dollos.mind import self_history as sh
    history_path = memory_root / "self_history.jsonl"
    slot_path = memory_root / "self_evolution" / "pending.json"
    current = sh.sanctioned_text(history_path)
    bundle, _new_off = assemble_bundle(memory_root=memory_root, hwm=hwm,
                                       window_days=window_days)
    task = _KEEPER_TASK.format(
        identity_self=pack_identity.self, personality=pack_identity.personality,
        current=current or "(尚無——這會是第一版)", bundle=bundle)
    try:
        report = await _run_keeper_agent(
            task=task, adapter=adapter, renderer=renderer, memsearch=memsearch,
            memory_root=memory_root, transcripts_root=transcripts_root,
            tool_output_store=tool_output_store, max_tokens=max_tokens)
        kind, text, rationale = parse_keeper_report(
            (report or {}).get("details", ""))
    except Exception:
        logger.exception("evolution keeper errored")
        evo.log_or_raise(history_path, kind=evo.EVO_ERROR, detail="keeper error")
        return "error"

    if kind == "no_change":
        evo.log_or_raise(history_path, kind=evo.EVO_NO_CHANGE, reason=text)
        return "no_change"

    reason = evo.mechanical_checks(text, floor=floor, cap=cap,
                                   enforcement=enforcement)
    if reason is not None:
        evo.log_or_raise(history_path, kind=evo.EVO_KILL,
                         reason=f"mechanical:{reason}", text=text)
        return "kill"

    try:
        verdict = await _run_full_skeptic(
            candidate=text, rationale=rationale, bundle=bundle, current=current,
            pack_identity=pack_identity, adapter=adapter, renderer=renderer,
            memsearch=memsearch, memory_root=memory_root,
            transcripts_root=transcripts_root,
            tool_output_store=tool_output_store, max_tokens=max_tokens)
    except Exception:
        logger.exception("evolution full skeptic errored")
        evo.log_or_raise(history_path, kind=evo.EVO_ERROR, detail="skeptic error")
        return "error"

    if verdict != "pass":
        evo.log_or_raise(history_path, kind=evo.EVO_KILL, text=text,
                         reason=verdict.split(":", 1)[1] if ":" in verdict else verdict)
        return "kill"

    # Log-before-mutate: birth line, then slot (Plan-2 invariant).
    evo.log_or_raise(history_path, kind=evo.EVO_CANDIDATE, text=text,
                     rationale=rationale, hwm_before=hwm)
    evo.save_slot(slot_path, evo.make_keeper_slot(
        candidate=text, rationale=rationale, hwm_before=hwm, created_ts=now))
    return "candidate"
```

**Required constant additions to `src/dollos/mind/evolution.py`** (verified absent today — plan review M2; add next to the existing evo_* constants):

```python
EVO_CANDIDATE = "evo_candidate"   # Mode A: keeper candidate born (Plan 3)
EVO_NO_CHANGE = "evo_no_change"   # Mode A: keeper found no coherent shift (Plan 3)
```

- [ ] **Step 4: Run** — 10 PASS + full suite green.
- [ ] **Step 5: Commit** — `feat(mind): Mode A keeper — evidence bundle + cite-or-die + (a)-(e) skeptic (evolution spec §3.3)`

---

### Task 4: Mode A in EvolutionTrigger + decision-event bookkeeping

**Files:**
- Modify: `src/dollos/mind/evolution_trigger.py` (ctor params + Mode A gate + run-loop branch + bookkeeping)
- Modify: `src/dollos/mind/evolution.py` (`surface_or_expire` gains optional `mind_state=None` — expire restores HWM + anchors `last_attempt`)
- Modify: `src/dollos/tools.py` (`SelfRevision` adopt/reject anchor `last_attempt` + interval via `ctx.mind_state`)
- Modify: `src/dollos/mind/mind_ctx.py` (+`evolution_base_interval_days: float = 7.0`, `evolution_max_interval_days: float = 28.0`)
- Modify: `src/dollos/mind/mind_loop.py` (pass `mind_state=self._state` into the `surface_or_expire` call)
- Modify: `src/dollos/kernel.py` (thread new EvolutionTrigger ctor params + MindCtx fields from `settings.evolution`)
- Modify: `tests/test_evolution_trigger.py` — **plan review C1**: `_StubState` gains the three new fields (`last_evolution_attempt_at=0.0`, `evolution_interval_days=0.0`, `evolution_hwm=0`); every `EvolutionTrigger(...)` construction gains the new ctor params + `persist_path=tmp_path / "mind_state.json"`. Mode-B BEHAVIOR stays untouched; only construction plumbing changes.
- Modify: `tests/test_evolution_integration.py` — **C1**: both `SimpleNamespace(last_user_at=…)` trigger states gain the three fields + ctor params; the `_ctx` helper gains `mind_state=MindState()`, `evolution_base_interval_days=7.0`, `evolution_max_interval_days=28.0`.
- Test: `tests/test_evolution_mode_a_trigger.py` (+ additions to `tests/test_self_revision.py` — same `_ctx` extension)

**Interfaces:**
- Consumes: Task 1 fields, Task 2 helpers, Task 3 `run_evolution_pass`.
- Produces: `EvolutionTrigger` ctor gains `persist_path`, `shared_dir`, `consolidated_window_days`(=`max_interval_days`), `base_interval_days`, `max_interval_days`, `min_history_events`, `min_diary_days`, `enforcement`, `floor`, `cap`; `_should_run_mode_a(now) -> bool`; Mode A branch in `run()`; bookkeeping applied per the §3.3 failure table.

Semantics to implement exactly:

1. **Init:** in `EvolutionTrigger.__init__`, if `state.last_evolution_attempt_at == 0.0` → set to `time.time()`; if `state.evolution_interval_days == 0.0` → set to `base_interval_days`; `save_state(state, persist_path)` (first boot waits a full base interval — spec §3.3 bootstrap). Additions the ctor also needs (review M3): `from dollos.mind.mind_state import save_state`; `self._mode_a_error_ts: float | None = None`; `shared_dir` derived internally as `memory_root / "shared"` (NOT a ctor param); the evidence window = `max_interval_days` (no separate knob); `_should_run_mode_a` null-guards `consolidation_trigger` exactly like `_should_reverdict`.
2. **`_should_run_mode_a(now)`** (checked only when `_should_reverdict` returned False): idle ≥ threshold; `now - state.last_evolution_attempt_at ≥ state.evolution_interval_days * 86400`; material gate = `evo.count_new_pin_events(history, state.evolution_hwm) >= min_history_events` OR `evo.diary_days_since(shared_dir, state.last_evolution_attempt_at) >= min_diary_days`; no consolidation running; `evo.load_slot(...) is None` (condition 5, either status); Mode-A error cooldown: `now - (self._mode_a_error_ts or 0) >= ERROR_COOLDOWN_S`.
3. **Run branch:** snapshot `hwm = state.evolution_hwm` and capture `new_off` via `evo.history_snapshot(history, hwm)[1]` BEFORE the pass (pass receives `hwm=hwm`; the pass's own evo_* lines land after `new_off` and pin-only counting makes that harmless); `outcome = await wait_for(run_evolution_pass(...), timeout=agent_timeout_s)` inside `current_task` (consolidation `_run_once` pattern). **`TimeoutError` → log `evo.log_or_raise(history, kind=evo.EVO_ERROR, detail="mode-a timeout")` FIRST, then treat as outcome `"error"`** (plan review I2 — the spec failure table lists timeout under evo_error; the cancelled pass wrote nothing, so the audit line must come from the trigger). **Import style (review M1):** `from dollos.mind.evolution_keeper import run_evolution_pass` and call the bare name — the Task-4 tests monkeypatch `et_mod.run_evolution_pass`.
4. **Bookkeeping by outcome** (trigger-side, then `save_state`):
   - `"no_change"` / `"kill"`: `last_attempt := now`; `interval := evo.next_interval_days(interval, outcome="evo_no_change"|"evo_kill", ...)`; `evolution_hwm := new_off` (verdicted — evidence consumed).
   - `"candidate"`: `last_attempt := now`; interval unchanged (the decision event will set it); `evolution_hwm := new_off`.
   - `"error"`: `self._mode_a_error_ts = now`; nothing else (not an attempt).
   - `CancelledError`: propagate to `run()`'s handler; nothing recorded (not an attempt).
5. **Decision events:**
   - `SelfRevision` adopt: after the existing clear-slot, `ctx.mind_state.last_evolution_attempt_at = time.time()`; `ctx.mind_state.evolution_interval_days = ctx.evolution_base_interval_days` (reset). Reject: anchor `last_attempt`; `interval := min(interval*2, ctx.evolution_max_interval_days)` — implement via `evo.next_interval_days`. (State persistence: mind_loop already saves state at turn end — verify; if not, call the existing save path the same way MoodTool-style mutations persist.)
   - `surface_or_expire` expire branch: when `mind_state is not None` → `mind_state.last_evolution_attempt_at = now`; interval unchanged; `if slot.hwm_before is not None: mind_state.evolution_hwm = slot.hwm_before` (restore — spec §3.3).
   - External-origin adopt/reject must NOT touch the interval (spec: external events leave it unchanged) — gate the interval update on `slot.kind != "external"`; the `last_attempt` anchor applies regardless.
   - The adopt-write-failure branch (tools.py F1 path: evo_adopt flushed, file write failed twice, slot cleared) ALSO logged a real `evo_adopt` — apply the same reset-to-base + anchor there (review M4: interval semantics follow the logged event, not the happy path).
   - **Diary-anchor asymmetry, consciously accepted (review I3):** the diary clause anchors to `last_evolution_attempt_at`, which advances on expire — so expired-candidate diary evidence does NOT re-seed (unlike pins, whose HWM restores from `hwm_before`). Accepted: the material gate is an OR, pins are the primary channel and their restoration alone re-seeds the pass; the next diary day re-fires the clause naturally. The spec is amended to record this (§3.3).

- [ ] **Step 1: Write the failing tests** — `tests/test_evolution_mode_a_trigger.py`:

```python
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
```

Plus in `tests/test_self_revision.py` (append): adopt resets `evolution_interval_days` to `ctx.evolution_base_interval_days` and anchors `last_evolution_attempt_at`; reject doubles the interval (cap respected); external-origin adopt leaves the interval unchanged. (Extend the file's stub ctx with `mind_state=MindState()`, `evolution_base_interval_days=7.0`, `evolution_max_interval_days=28.0` — match its existing stub style.)

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** per the semantics block above. `run()` order: `_should_reverdict` first (Mode B priority — an awaiting_skeptic slot is condition-5-blocking anyway); else `_should_run_mode_a(now)` → `await self._run_mode_a_once(now)` (which wraps `run_evolution_pass` in `create_task(wait_for(...))` following `ConsolidationTrigger._run_once`, maps `TimeoutError → "error"`, applies bookkeeping, `save_state`). Kernel threads the new params from `settings.evolution` + `settings.data`; MindCtx gains the two interval knobs (kernel wires them).
- [ ] **Step 4: Run** — new tests + `tests/test_evolution_trigger.py` (Mode B untouched) + full suite green. FULL suite mandatory: kernel wiring changed.
- [ ] **Step 5: Commit** — `feat(mind): EvolutionTrigger Mode A — gate, bookkeeping, decision-event anchoring (evolution spec §3.3)`

---

### Task 5: full-loop integration tests

**Files:**
- Create: `tests/test_evolution_full_loop.py`

**Interfaces:** consumes everything above; stubs ONLY `_run_keeper_agent`/`_run_full_skeptic` (the LLM boundary).

- [ ] **Step 1: Write the tests** — three integration tests driving real modules end-to-end:

```python
# tests/test_evolution_full_loop.py
"""Plan-3 full loop: pins → Mode A pass → keeper slot → surfacing → adopt (spec §6)."""
import asyncio

import pytest

from dollos.mind import evolution as evo, evolution_keeper as ek, self_history
from dollos.mind.mind_state import MindState

VALID = "我最近發現自己會主動整理系統日誌,一行行看下去有種踏實感,不再只是等主人開口才動,"\
        "遇到看不懂的紀錄還會自己追下去查清楚才安心。" + "細節" * 9   # 81 chars ≥ floor 80 (review I1)


@pytest.mark.asyncio
async def test_full_loop_candidate_to_adopt(tmp_path, monkeypatch):
    from dollos.character import Enforcement, Identity
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
    ident = Identity(self="我是測試角色", personality="安靜", taboos="不編造")
    out = await ek.run_evolution_pass(
        adapter=None, renderer=None, memsearch=None, memory_root=mr,
        transcripts_root=mr / "tx", tool_output_store=None, pack_identity=ident,
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
    import types
    from dollos.tools import SelfRevision
    from dollos.character import Enforcement as Enf
    ctx = types.SimpleNamespace(
        memory_root=mr, evolution_latched=False, evolution_candidate_surfaced=True,
        enforcement=Enf(), current_self_min_chars=80, current_self_max_chars=600,
        mind_state=MindState(), evolution_base_interval_days=7.0,
        evolution_max_interval_days=28.0)
    result = await SelfRevision(decision="adopt", text="", reason="").run(ctx)
    assert "採納" in result
    assert self_history.sanctioned_text(hist) == VALID
    assert (mr / "current_self.md").read_text(encoding="utf-8") == VALID
    assert ctx.mind_state.evolution_interval_days == 7.0   # reset on adopt
```

(Adjust the SimpleNamespace stub to whatever `tests/test_self_revision.py` uses — copy its helper. Add two more tests: `test_full_loop_no_change_starves_quietly` (fake keeper NO_CHANGE → no slot, evo_no_change logged) and `test_full_loop_skeptic_kill_no_slot`.)

- [ ] **Step 2-4:** run → implement nothing (test-only; if a genuine bug surfaces, STOP and report) → full suite green.
- [ ] **Step 5: Commit** — `test(mind): Plan-3 full-loop integration (evolution spec §6)`

---

## Completion

1. Full suite green → final whole-branch review (multi-lens, per Plan-2 precedent).
2. **Live smoke (merge gate, 軟機制必 live smoke):** extend the Plan-2 harness — seed real pin churn + a diary file in a scratch root, clamp `base_interval_days≈0.00003` (≈3s), `min_history_events=3`, `idle_threshold_s=1`; let Mode A fire against the real llama-server; verdict criteria: (a) keeper Report is GROUNDED (依據 cites the seeded events — RP filler = FAIL); (b) full-scope skeptic passes a grounded candidate / kills a fabricated-citation probe; (c) the surfaced candidate is adopted by the real model and renders next turn; (d) no_change path fires when the scratch root has no material (寧缺勿濫 verified live).
3. Merge via `superpowers:finishing-a-development-branch`; update `docs/roadmap.md` + CLAUDE.md plan tables (Plans 1-3 = 慢變演化 complete); record deferred items (§7 of the spec) in the roadmap's 下一個 section.
