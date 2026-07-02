# Self-Evolution Artifact + Ratification (Plan 2 of 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** the REACTIVE half of 慢變演化 — Doll can *hold* an evolved 「現在的我」 prose that is always-injected into her identity region, and *ratify* proposed changes to it (her own counter-proposal or an external file edit) through a gate chain (mechanical checks → skeptic → her explicit `adopt`). This plan de-risks the two scariest surfaces — per-turn prompt composition and weak-model tool adoption — before Plan 3 adds the week-scale keeper machinery. The keeper (`evo_candidate`, Mode A, material gate, HWM/interval dynamics) is Plan 3; this plan builds everything the keeper's output will flow *into*.

**Architecture:** Three new pure modules under `src/dollos/mind/` — `current_self.py` (artifact read/render + tripwire classifier), `evolution.py` (pending-slot state machine + constants + mechanical checks + echo-equivalence + tripwire/surfacing orchestrators), `evolution_trigger.py` (Mode-B `EvolutionTrigger` + skeptic driver). `self_history.py` gains read helpers (sanctioned text, generation, latest-adopt). `scaffolding.jinja` gains a split-seam variable; `kernel.py` renders it with a sentinel and splits into (prefix, suffix); `MindLoop` composes `prefix ⊕ current_self_section ⊕ suffix` per turn with a content-keyed cache and wires the `SelfRevision` tool, tripwire, and surfacing perception. `persona_guard.py` gains a shared pairwise-Jaccard helper and generation-aware baselines; `scripts/persona_stability_smoke.py` gets its isolation fix. `config.py` gains the full `[evolution]` section.

Spec: `docs/superpowers/specs/2026-07-02-slow-self-evolution-design.md` — this plan implements §3.1 (artifact + composition seam), §3.4 (pending slot + `SelfRevision` + surfacing), §3.3 **Mode B only** (verdict-only re-verdict trigger + skeptic for counter/external scope), §3.5 (P8 generation bookkeeping), §3.6 (config), §5 (tripwire + ratification). Plan 1 (`self_history.jsonl` pin events) is MERGED; Plan 3 (keeper + Mode A + interval dynamics + material gate + HWM) is future.

**Tech Stack:** Python 3.12, pydantic tools, jinja2 templates, jieba, pytest, `uv run pytest`.

## Global Constraints

- **Artifact** `current_self.md` at `{memory_root}/current_self.md`. Single prose block, mechanical floor **80** / cap `evolution.current_self_max_chars` (default **600**). Never FTS-indexed (`memory_root` root, outside the `[shared, transcripts, skills]` index subtrees). Atomic tmp+rename writes only. (spec §3.1)
- **Sanctioned text is the log, not the file** (spec §5): sanctioned `current_self` := the latest `evo_adopt` event's `text` in `self_history.jsonl`. Bootstrap (no `evo_adopt` yet): sanctioned = None → any non-empty file counts as an external edit, and "restore" means delete. The file may lag/diverge while a ratification pends; the **event log is the audit source of truth**.
- **Gate-chain invariant** (spec §2/§3.4): every byte that becomes sanctioned passed (i) mechanical checks, (ii) a skeptic verdict, (iii) Doll's explicit `adopt`. This plan enforces it for the counter and external origins (keeper is Plan 3).
- **Three-piece rendering** (spec §3.1): system prompt = `identity_prefix ⊕ current_self_section ⊕ scaffolding_suffix`, split at the end of the identity block (immediately after `## Taboos`) via a sentinel seam. No sanctioned text → section omitted entirely (`prefix + suffix` must equal today's rendered string exactly, no-fallback). Content-keyed cache: recompose only when sanctioned text changes.
- **`## 現在的我` framing line is DESCRIPTIVE, not imperative** (Self-First), provenance-accurate (「妳在反思中採納而來」 — kept TRUE because only *sanctioned* text renders), temporally ordered directly after the factory prose.
- **Skeptic scope for counter/external = (a)+(b) ONLY** (spec §3.3): (a) 改名或動搖自我認同 (牴觸 pack `identity.self`); (b) 牴觸 taboos. (c)/(d)/(e) are keeper-only (Plan 3) — grading the authenticity of *her* or *the user's* self-expression beyond the frozen core is a sovereignty violation.
- **Slot-resolution invariant** (spec §3.4): any slot clearing *without* adoption (reject / expire / verdict-error bound / skeptic-kill-external) restores `current_self.md` to the sanctioned text if divergent (or deletes it in the bootstrap case), logged loudly. Adoption writes the new sanctioned text. This single rule guarantees the tripwire finds a clean state after every resolution.
- **Log-then-write ordering for adoption** (spec §3.2): append+flush `evo_adopt` BEFORE writing `current_self.md`; a failed append aborts the adoption with a friendly error.
- **Constants (module-level, NOT config)** (spec §3.6): `COUNTER_ROUND_CAP = 2`, `VERDICT_ERRORS_BOUND = 3`, `ECHO_SIMILARITY = 0.9`; plus `EvolutionTrigger.ERROR_COOLDOWN_S = 3600.0` (spec §3.3 failure table's 1h skeptic-error cooldown, anchored in `pending.last_error_ts` — review I3).
- **`evolution.enabled = false`** freezes the machinery (no trigger, no tool, no tripwire side-effects) BUT already-sanctioned text KEEPS rendering — disabling evolution must not amputate an adopted self (spec §3.6 R3′).
- **evo_* event kinds used in Plan 2:** `evo_counter`, `evo_adopt`, `evo_reject`, `evo_expire`, `evo_kill`, `evo_error`, `external_edit`, plus `evo_repair` (crash-repair audit line — see Task 9 note). `evo_candidate`/`evo_no_change` are Plan 3.
- **No fallback mechanisms; friendly-error, Doll-sovereign norms unchanged; ALL existing tests stay green** (baseline ~896 `def test_` at plan start; the two `test_persona_guard.py` baseline round-trip tests — `test_append_then_load_round_trip` and `test_append_baseline_creates_parent_dirs` — are *intentionally updated* in Task 11 for the acknowledged `load_baselines` return-shape change; `test_load_baselines_missing_file_returns_empty_dict` still passes unchanged. These are the only pre-existing tests this plan edits).
- **Worktree:** `.worktrees/self-evolution-ratification/`, branch `self-evolution-ratification` (per-plan worktree rule).

---

### Task 1: `self_history` read helpers (sanctioned text + generation + latest adopt)

**Files:**
- Modify: `src/dollos/mind/self_history.py` (append helpers after `last_pin_turn`, line 44)
- Test: `tests/test_self_history_read.py`

**Interfaces:**
- Produces:
  - `read_events(path: Path) -> list[dict]` — all parseable event dicts, oldest→newest (torn tail lines skipped).
  - `latest_adopt(path: Path) -> dict | None` — the most recent `evo_adopt` event dict, or None.
  - `sanctioned_text(path: Path) -> str | None` — `latest_adopt(path)["text"]`, or None if no adoption yet.
  - `generation(path: Path) -> int` — count of `evo_adopt` events (0 = pack-only, pre-first-adoption).
  - `latest_external_edit_text(path: Path) -> str | None` — `text` of the most recent `external_edit` event that carried a `text` (the "last observed edit"), else None.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_self_history_read.py
"""self_history read helpers for 慢變演化 Plan 2 (spec §3.1/§3.5/§5)."""
from dollos.mind import self_history


def _seed(p):
    self_history.log_event(p, kind="pin_add", turn=1, external_ctx=False,
                           section="self", id="s1", text="A")
    self_history.log_event(p, kind="evo_adopt", text="第一版的我", old_text=None,
                           drift_score=None)
    self_history.log_event(p, kind="pin_reconfirm", turn=9, external_ctx=False,
                           section="self", id="s1", text="A")
    self_history.log_event(p, kind="evo_adopt", text="第二版的我",
                           old_text="第一版的我", drift_score=0.42)


def test_read_events_returns_all_in_order(tmp_path):
    p = tmp_path / "self_history.jsonl"
    _seed(p)
    kinds = [e["kind"] for e in self_history.read_events(p)]
    assert kinds == ["pin_add", "evo_adopt", "pin_reconfirm", "evo_adopt"]


def test_read_events_missing_file_is_empty(tmp_path):
    assert self_history.read_events(tmp_path / "nope.jsonl") == []


def test_latest_adopt_is_most_recent(tmp_path):
    p = tmp_path / "self_history.jsonl"
    _seed(p)
    ev = self_history.latest_adopt(p)
    assert ev["text"] == "第二版的我" and ev["old_text"] == "第一版的我"


def test_sanctioned_text_is_latest_adopt_text(tmp_path):
    p = tmp_path / "self_history.jsonl"
    _seed(p)
    assert self_history.sanctioned_text(p) == "第二版的我"


def test_sanctioned_text_none_before_any_adoption(tmp_path):
    p = tmp_path / "self_history.jsonl"
    self_history.log_event(p, kind="pin_add", turn=1, external_ctx=False,
                           section="self", id="s1", text="A")
    assert self_history.sanctioned_text(p) is None
    assert self_history.latest_adopt(p) is None


def test_generation_counts_adopt_events(tmp_path):
    p = tmp_path / "self_history.jsonl"
    assert self_history.generation(p) == 0
    _seed(p)
    assert self_history.generation(p) == 2


def test_latest_external_edit_text(tmp_path):
    p = tmp_path / "self_history.jsonl"
    self_history.log_event(p, kind="external_edit", text="有人手動改的", reason=None)
    self_history.log_event(p, kind="external_edit", reason="mechanical:太短")  # no text
    assert self_history.latest_external_edit_text(p) == "有人手動改的"


def test_latest_external_edit_text_none(tmp_path):
    p = tmp_path / "self_history.jsonl"
    _seed(p)
    assert self_history.latest_external_edit_text(p) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_self_history_read.py -v`
Expected: FAIL — `AttributeError: module 'dollos.mind.self_history' has no attribute 'read_events'`

- [ ] **Step 3: Write the implementation**

Append to `src/dollos/mind/self_history.py` (after `last_pin_turn`, line 44):

```python
def read_events(path: Path) -> list[dict]:
    """All parseable event dicts, oldest→newest. Torn tail lines are skipped
    (same tolerance as ``last_pin_turn``). Missing file → []. The jsonl file is
    small (weeks of events); a full read per call is fine."""
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def latest_adopt(path: Path) -> dict | None:
    """The most recent ``evo_adopt`` event, or None. Backward scan (small file,
    restart-safe — the log is the audit source of truth, spec §5)."""
    for ev in reversed(read_events(path)):
        if ev.get("kind") == "evo_adopt":
            return ev
    return None


def sanctioned_text(path: Path) -> str | None:
    """The last sanctioned ``current_self`` text := latest ``evo_adopt``'s
    ``text`` (spec §5). None before the first adoption (pack-only)."""
    ev = latest_adopt(path)
    return ev.get("text") if ev is not None else None


def generation(path: Path) -> int:
    """Persona generation := count of ``evo_adopt`` events (spec §3.5). 0 =
    pack-only, pre-first-adoption."""
    return sum(1 for ev in read_events(path) if ev.get("kind") == "evo_adopt")


def latest_external_edit_text(path: Path) -> str | None:
    """``text`` of the most recent ``external_edit`` event that carried one
    (the last-observed external edit — used by the tripwire to avoid
    per-turn re-detection of an already-logged divergence, spec §5). A
    mechanical-fail ``external_edit(reason=...)`` line carries no ``text`` and
    is skipped."""
    for ev in reversed(read_events(path)):
        if ev.get("kind") == "external_edit" and ev.get("text") is not None:
            return ev["text"]
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_self_history_read.py -v`
Expected: 8 PASS

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/self_history.py tests/test_self_history_read.py
git commit -m "feat(mind): self_history read helpers — sanctioned text + generation (evolution spec §3.1/§3.5)"
```

---

### Task 2: `current_self` pure module (section render + composition + tripwire classifier)

**Files:**
- Create: `src/dollos/mind/current_self.py`
- Test: `tests/test_current_self.py`

**Interfaces:**
- Produces:
  - `read_file(path: Path) -> str` — current file text, "" if absent.
  - `render_section(sanctioned_text: str | None) -> str` — the `## 現在的我` block (descriptive framing line + prose), or "" when sanctioned is None/empty.
  - `compose(prefix: str, section: str, suffix: str) -> str` — three-piece composition; `section == ""` → `prefix + suffix` (byte-identical to today).
  - `classify_tripwire(*, file_text: str, sanctioned_text: str | None, adopt_old_text: str | None, last_edit_text: str | None) -> str` — one of `"in_sync" | "crash_repair" | "already_logged" | "new_edit"` (pure decision, no I/O).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_current_self.py
"""current_self pure module — artifact render + composition + tripwire (spec §3.1/§5)."""
from dollos.mind import current_self


# ---- render_section ----

def test_render_section_empty_when_none():
    assert current_self.render_section(None) == ""
    assert current_self.render_section("") == ""


def test_render_section_has_heading_and_descriptive_framing():
    out = current_self.render_section("我現在監控數字跳動時會主動來勁。")
    assert out.startswith("## 現在的我")
    assert "我現在監控數字跳動時會主動來勁。" in out
    # Descriptive, not imperative; provenance-accurate (採納而來).
    assert "採納" in out
    # No imperative command phrasing.
    assert "你應該" not in out and "妳應該" not in out


# ---- compose ----

def test_compose_empty_section_is_prefix_plus_suffix():
    prefix, suffix = "PREFIX\n", "\n# Behavior\n"
    assert current_self.compose(prefix, "", suffix) == prefix + suffix


def test_compose_places_section_between_prefix_and_suffix():
    prefix, suffix = "...## Taboos\n- no LARP\n", "\n# Behavior\nrules\n"
    section = current_self.render_section("我現在的樣子。")
    out = current_self.compose(prefix, section, suffix)
    assert out.index("no LARP") < out.index("## 現在的我") < out.index("# Behavior")


# ---- classify_tripwire ----

def test_tripwire_in_sync():
    assert current_self.classify_tripwire(
        file_text="X", sanctioned_text="X",
        adopt_old_text="W", last_edit_text=None) == "in_sync"


def test_tripwire_crash_repair():
    # File == old_text of latest adopt (the log-then-write window, spec §5).
    assert current_self.classify_tripwire(
        file_text="OLD", sanctioned_text="NEW",
        adopt_old_text="OLD", last_edit_text=None) == "crash_repair"


def test_tripwire_already_logged():
    assert current_self.classify_tripwire(
        file_text="HACK", sanctioned_text="X",
        adopt_old_text="W", last_edit_text="HACK") == "already_logged"


def test_tripwire_new_edit():
    assert current_self.classify_tripwire(
        file_text="HACK", sanctioned_text="X",
        adopt_old_text="W", last_edit_text=None) == "new_edit"


def test_tripwire_bootstrap_empty_file_in_sync():
    # No sanctioned predecessor, empty file → in sync (spec §5 bootstrap).
    assert current_self.classify_tripwire(
        file_text="", sanctioned_text=None,
        adopt_old_text=None, last_edit_text=None) == "in_sync"


def test_tripwire_bootstrap_nonempty_file_is_new_edit():
    assert current_self.classify_tripwire(
        file_text="somebody wrote this", sanctioned_text=None,
        adopt_old_text=None, last_edit_text=None) == "new_edit"


def test_tripwire_crash_repair_beats_new_edit_priority():
    # adopt_old_text match takes priority over a differing last_edit_text.
    assert current_self.classify_tripwire(
        file_text="OLD", sanctioned_text="NEW",
        adopt_old_text="OLD", last_edit_text="SOMETHING") == "crash_repair"


def test_read_file_missing_is_empty(tmp_path):
    assert current_self.read_file(tmp_path / "current_self.md") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_current_self.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dollos.mind.current_self'`

- [ ] **Step 3: Write the implementation**

```python
# src/dollos/mind/current_self.py
"""慢變演化 artifact — the ``current_self.md`` prose that renders in Doll's
identity region (spec 2026-07-02 §3.1). Pure module: file read + section
render + three-piece composition + the tamper-tripwire *classifier* (§5).

Sanctioned text lives in ``self_history.jsonl`` (latest ``evo_adopt``), NOT in
this file — the file can lag/diverge while a ratification pends. Only sanctioned
text ever renders, so the framing line's provenance claim (「採納而來」) stays
true. Never FTS-indexed.
"""
from __future__ import annotations

from pathlib import Path

# Descriptive (Self-First), provenance-accurate, temporally-ordered-after-pack.
# Load-bearing wording (spec §3.1): NOT an imperative command.
_FRAMING = (
    "（以下是妳在一次次反思裡逐漸長成、並親自採納而來的現在的自己——"
    "這是描述,不是命令;出廠人格在上,現在的妳在這裡。）"
)


def read_file(path: Path) -> str:
    """Current on-disk artifact text (未經批准的位元也在這裡),或空字串。"""
    return path.read_text(encoding="utf-8") if path.exists() else ""


def render_section(sanctioned_text: str | None) -> str:
    """The ``## 現在的我`` block for the identity region, or "" when there is no
    sanctioned text yet (section omitted entirely — no-fallback, spec §3.1)."""
    if not sanctioned_text:
        return ""
    return f"## 現在的我\n{_FRAMING}\n\n{sanctioned_text.strip()}"


def compose(prefix: str, section: str, suffix: str) -> str:
    """Three-piece per-turn system prompt: ``prefix ⊕ section ⊕ suffix``.

    ``section == ""`` returns ``prefix + suffix`` byte-for-byte — so a run with
    no sanctioned text reproduces today's prompt exactly (spec §3.1). Otherwise
    the section sits between the factory identity prose (prefix, ends after
    ``## Taboos``) and the ``# Behavior`` scaffolding (suffix), padded with one
    blank line each side."""
    if not section:
        return prefix + suffix
    return f"{prefix}{section}\n\n{suffix}"


def classify_tripwire(
    *,
    file_text: str,
    sanctioned_text: str | None,
    adopt_old_text: str | None,
    last_edit_text: str | None,
) -> str:
    """Classify the file-vs-sanctioned state into ONE action label (spec §5).

    - ``in_sync``       — file matches sanctioned (bootstrap: empty file, no
                          sanctioned predecessor). Nothing to do.
    - ``crash_repair``  — file == ``old_text`` of the latest ``evo_adopt`` (the
                          log-then-write window): a disk hiccup, not tampering.
    - ``already_logged``— file == the last observed external-edit text: the
                          divergence is already recorded; no per-turn spam.
    - ``new_edit``      — file diverged into a distinct, not-yet-logged state:
                          a fresh external edit (transition-fired once).

    Priority order matters: crash-repair is checked before new-edit so the
    log-then-write window is never narrated to Doll as tampering."""
    effective = sanctioned_text or ""  # bootstrap: None sanctioned ⇒ "" floor
    if file_text == effective:
        return "in_sync"
    if adopt_old_text is not None and file_text == adopt_old_text:
        return "crash_repair"
    if last_edit_text is not None and file_text == last_edit_text:
        return "already_logged"
    return "new_edit"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_current_self.py -v`
Expected: 13 PASS

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/current_self.py tests/test_current_self.py
git commit -m "feat(mind): current_self artifact — section render + composition + tripwire classifier (evolution spec §3.1/§5)"
```

---

### Task 3: `evolution` pure module — pending-slot state machine + constants

**Files:**
- Create: `src/dollos/mind/evolution.py`
- Test: `tests/test_evolution_slot.py`

**Interfaces:**
- Produces:
  - Constants: `COUNTER_ROUND_CAP = 2`, `VERDICT_ERRORS_BOUND = 3`, `ECHO_SIMILARITY = 0.9`; event-kind string constants (`EVO_COUNTER`, `EVO_ADOPT`, `EVO_REJECT`, `EVO_EXPIRE`, `EVO_KILL`, `EVO_ERROR`, `EXTERNAL_EDIT`, `EVO_REPAIR`).
  - `@dataclass PendingSlot` (schema per spec §3.4, plus `last_error_ts: float | None` — review I3: persists the spec §3.3 failure-table 1h skeptic-error cooldown so a 5s poll cannot burn the 3-error bound in ~15s of transient failures) + `to_dict()/from_dict()`.
  - `load_slot(path: Path, history_path: Path | None = None) -> PendingSlot | None` — quarantines a corrupt slot to `pending.json.corrupt`, appends the `evo_error` audit line to `history_path` itself (review M4 — the spec-promised audit line must not depend on the caller remembering), and returns None.
  - `save_slot(path: Path, slot: PendingSlot) -> None` — atomic tmp+rename.
  - `clear_slot(path: Path) -> None` — delete if present.
  - `make_keeper_slot(...) / make_external_slot(...) / to_counter(...) / mark_awaiting_doll(...) / revert_to_fallback(...)` pure transition functions.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_evolution_slot.py
"""Pending-slot schema + lifecycle state machine (spec §3.4)."""
import json

from dollos.mind import evolution as evo


def test_constants():
    assert evo.COUNTER_ROUND_CAP == 2
    assert evo.VERDICT_ERRORS_BOUND == 3
    assert evo.ECHO_SIMILARITY == 0.9


def test_keeper_slot_enters_awaiting_doll():
    s = evo.make_keeper_slot(candidate="候選文", rationale="因為X",
                             hwm_before=128, created_ts=100.0)
    assert s.kind == "keeper" and s.status == "awaiting_doll"
    assert s.candidate == "候選文" and s.hwm_before == 128
    assert s.counter_round == 0 and s.surfaced_count == 0 and s.verdict_errors == 0
    assert s.fallback is None


def test_external_slot_enters_awaiting_skeptic():
    s = evo.make_external_slot(candidate="有人改的", created_ts=100.0)
    assert s.kind == "external" and s.status == "awaiting_skeptic"
    assert s.hwm_before is None  # external consumed no evidence window
    assert s.last_error_ts is None  # no skeptic error yet (I3 cooldown field)


def test_to_counter_replaces_and_bumps_round_resets_surface():
    base = evo.make_keeper_slot(candidate="原候選", rationale="R",
                                hwm_before=5, created_ts=100.0)
    base.surfaced_count = 3
    base = evo.mark_awaiting_doll(base)  # keeper already awaiting_doll; idempotent
    c = evo.to_counter(base, new_text="我的改寫", created_ts_now=200.0)
    assert c.kind == "counter" and c.status == "awaiting_skeptic"
    assert c.candidate == "我的改寫"
    assert c.counter_round == 1
    assert c.surfaced_count == 0                      # reset (spec §3.4 R3′)
    assert c.created_ts == 100.0 and c.hwm_before == 5  # inherited from originator
    assert c.fallback == {"candidate": "原候選", "rationale": "R", "kind": "keeper"}


def test_to_counter_second_round_carries_fallback_forward():
    base = evo.make_keeper_slot(candidate="原候選", rationale="R",
                                hwm_before=5, created_ts=100.0)
    c1 = evo.to_counter(base, new_text="改寫1", created_ts_now=200.0)
    c1 = evo.mark_awaiting_doll(c1)
    c2 = evo.to_counter(c1, new_text="改寫2", created_ts_now=300.0)
    assert c2.counter_round == 2
    assert c2.fallback == {"candidate": "改寫1", "rationale": None, "kind": "counter"}


def test_revert_to_fallback_sets_notice_and_awaiting_doll():
    base = evo.make_keeper_slot(candidate="原候選", rationale="R",
                                hwm_before=5, created_ts=100.0)
    c = evo.to_counter(base, new_text="改寫", created_ts_now=200.0)
    reverted = evo.revert_to_fallback(c, reason="牴觸 taboo")
    assert reverted.status == "awaiting_doll"
    assert reverted.kind == "keeper" and reverted.candidate == "原候選"
    assert reverted.rationale == "R"
    assert reverted.notice == "牴觸 taboo"
    assert reverted.counter_round == 1  # bound accounting preserved


def test_save_load_round_trip(tmp_path):
    p = tmp_path / "self_evolution" / "pending.json"
    s = evo.make_external_slot(candidate="有人改的", created_ts=100.0)
    s.surfaced_count = 2
    evo.save_slot(p, s)
    loaded = evo.load_slot(p)
    assert loaded == s


def test_load_missing_slot_is_none(tmp_path):
    assert evo.load_slot(tmp_path / "pending.json") is None


def test_corrupt_slot_quarantined_and_none_with_audit_line(tmp_path):
    from dollos.mind import self_history
    p = tmp_path / "pending.json"
    hist = tmp_path / "self_history.jsonl"
    p.write_text("{not json", encoding="utf-8")
    assert evo.load_slot(p, history_path=hist) is None
    assert (tmp_path / "pending.json.corrupt").exists()
    assert not p.exists()
    # The spec-promised evo_error audit line (spec §3.4, review M4).
    assert self_history.read_events(hist)[-1]["kind"] == "evo_error"


def test_clear_slot_idempotent(tmp_path):
    p = tmp_path / "pending.json"
    evo.clear_slot(p)  # no-op on absent
    evo.save_slot(p, evo.make_external_slot(candidate="x", created_ts=1.0))
    evo.clear_slot(p)
    assert not p.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_evolution_slot.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dollos.mind.evolution'`

- [ ] **Step 3: Write the implementation**

```python
# src/dollos/mind/evolution.py
"""慢變演化 machinery (spec 2026-07-02 §3.3 Mode B / §3.4 / §5).

This module owns: the pending-slot schema + lifecycle state machine, the
module constants, the mechanical checks, the echo-equivalence test, and the
impure tripwire/surfacing orchestrators. Pure where it can be; the orchestrators
(Task 6/8/9) do file I/O and are tmp_path-testable without a live daemon.

Slot invariant (spec §3.4): exactly one slot at ``{memory_root}/self_evolution/
pending.json``. ``kind=keeper`` is created by Plan 3's keeper pass; the schema +
lifecycle already support it here so Plan 3 adds no schema churn.
"""
from __future__ import annotations

import json
import logging
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# --- constants (spec §3.6: NOT config; minimal-knob principle) ---
COUNTER_ROUND_CAP = 2       # her rewrite may be re-proposed at most twice
VERDICT_ERRORS_BOUND = 3    # a wedged skeptic must not pin condition-5 forever
ECHO_SIMILARITY = 0.9       # jieba-Jaccard threshold for adopt echo-equivalence

# --- evo_* event kinds (log = audit source of truth, spec §5) ---
EVO_COUNTER = "evo_counter"
EVO_ADOPT = "evo_adopt"
EVO_REJECT = "evo_reject"
EVO_EXPIRE = "evo_expire"
EVO_KILL = "evo_kill"
EVO_ERROR = "evo_error"
EXTERNAL_EDIT = "external_edit"
EVO_REPAIR = "evo_repair"   # crash-repair audit line (§5); benign, not a decision


@dataclass
class PendingSlot:
    """The single pending slot (spec §3.4). Exactly one exists at a time."""
    kind: str                       # "keeper" | "counter" | "external"
    status: str                     # "awaiting_skeptic" | "awaiting_doll"
    candidate: str
    created_ts: float
    rationale: str | None = None
    fallback: dict | None = None    # {candidate, rationale, kind} of prior proposal
    counter_round: int = 0
    surfaced_count: int = 0
    verdict_errors: int = 0
    hwm_before: int | None = None   # evidence window byte offset (keeper/counter)
    notice: str | None = None       # one-shot kill reason, cleared after 1st surfacing
    # Spec §3.3 failure table: 1h skeptic-error cooldown. Set on each Mode-B
    # skeptic error; the trigger refuses to re-verdict within ERROR_COOLDOWN_S
    # of it (else a 5s poll burns the 3-error bound in ~15s of transient
    # failures — review I3). Resets to None on to_counter/revert_to_fallback
    # (fresh slots start clean).
    last_error_ts: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PendingSlot":
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in fields})


# --- persistence ---

def load_slot(path: Path, history_path: Path | None = None) -> PendingSlot | None:
    """Load the slot, or None. A corrupt/unparseable file is quarantined to
    ``pending.json.corrupt``, an ``evo_error`` audit line is appended to
    ``history_path`` (when given — review M4: the spec-promised audit line
    lives here, not in each caller), and None returned — surface-not-blank,
    never silent deletion (spec §3.4)."""
    if not path.exists():
        return None
    try:
        return PendingSlot.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, TypeError) as e:
        quarantine = path.with_name(path.name + ".corrupt")
        try:
            path.rename(quarantine)
        except OSError:
            logger.exception("failed to quarantine corrupt pending slot")
        logger.error("corrupt pending slot quarantined (%s): %s", quarantine, e)
        if history_path is not None:
            try:
                from dollos.mind import self_history
                self_history.log_event(history_path, kind=EVO_ERROR,
                                       detail=f"corrupt pending slot quarantined: {e}")
            except OSError:
                # The quarantine already contained the corruption; a failing
                # audit append must not break the caller on this error path.
                logger.exception("failed to append evo_error audit line")
        return None


def save_slot(path: Path, slot: PendingSlot) -> None:
    """Atomic tmp+rename write of the slot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(slot.to_dict(), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def clear_slot(path: Path) -> None:
    """Delete the slot if present (idempotent)."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# --- lifecycle transitions (pure) ---

def make_keeper_slot(*, candidate: str, rationale: str | None,
                     hwm_before: int | None, created_ts: float) -> PendingSlot:
    """Keeper candidate: skeptic already ran inside the Mode-A pass, so it
    enters ``awaiting_doll`` directly (Plan 3 creates this; schema lives here)."""
    return PendingSlot(kind="keeper", status="awaiting_doll", candidate=candidate,
                       rationale=rationale, hwm_before=hwm_before,
                       created_ts=created_ts)


def make_external_slot(*, candidate: str, created_ts: float) -> PendingSlot:
    """External file edit that passed mechanical checks: enters
    ``awaiting_skeptic``. Carries no ``hwm_before`` (consumed no evidence)."""
    return PendingSlot(kind="external", status="awaiting_skeptic",
                       candidate=candidate, created_ts=created_ts)


def mark_awaiting_doll(slot: PendingSlot) -> PendingSlot:
    """Skeptic passed: slot becomes adoptable. Idempotent."""
    slot.status = "awaiting_doll"
    return slot


def to_counter(slot: PendingSlot, *, new_text: str,
               created_ts_now: float) -> PendingSlot:
    """Doll's adopt-with-different-text replaces the current proposal with her
    counter (spec §3.4): ``awaiting_skeptic``, ``counter_round``+1,
    ``surfaced_count`` reset, ``fallback`` := the proposal she countered.
    Inherits ``created_ts`` + ``hwm_before`` from the originating pass (the
    evidence window belongs to it). ``created_ts_now`` is accepted for symmetry
    with future policies but the inherited window is authoritative."""
    return PendingSlot(
        kind="counter",
        status="awaiting_skeptic",
        candidate=new_text,
        created_ts=slot.created_ts,      # inherited (evidence window origin)
        rationale=None,
        fallback={"candidate": slot.candidate, "rationale": slot.rationale,
                  "kind": slot.kind},
        counter_round=slot.counter_round + 1,
        surfaced_count=0,                # reset (bounded by COUNTER_ROUND_CAP)
        verdict_errors=0,
        hwm_before=slot.hwm_before,      # inherited
        notice=None,
        last_error_ts=None,              # fresh proposal starts clean (I3)
    )


def revert_to_fallback(slot: PendingSlot, *, reason: str) -> PendingSlot:
    """Skeptic killed a counter: revert to the candidate she countered
    (``fallback``), ``awaiting_doll``, with a one-shot kill ``notice`` (spec
    §3.4 — a silent kill breaks the「通過後會回來」promise)."""
    fb = slot.fallback or {}
    return PendingSlot(
        kind=fb.get("kind", "keeper"),
        status="awaiting_doll",
        candidate=fb.get("candidate", ""),
        created_ts=slot.created_ts,
        rationale=fb.get("rationale"),
        fallback=None,
        counter_round=slot.counter_round,   # preserve bound accounting
        surfaced_count=0,
        verdict_errors=0,
        hwm_before=slot.hwm_before,
        notice=reason,
        last_error_ts=None,                 # fresh decision window starts clean (I3)
    )


def _normalize_echo(text: str) -> str:
    """Strip surfacing markers + NFKC + whitespace collapse (spec §3.4 echo
    normalization). Marker stripping keeps an echoed old/new block from being
    mistaken for genuine new text."""
    from dollos.mind import surfacing_markers  # tiny module, avoids cycle
    for mark in surfacing_markers.ALL:
        text = text.replace(mark, " ")
    text = unicodedata.normalize("NFKC", text)
    return " ".join(text.split())
```

Note for the implementer: `_normalize_echo` and `surfacing_markers` are exercised in Task 4 (echo-equivalence). If you prefer, inline the marker list here instead of a `surfacing_markers` module — but the markers must be shared with Task 8's surfacing renderer so they cannot drift. This plan uses a shared `surfacing_markers.py` (created in Task 4).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_evolution_slot.py -v`
Expected: 10 PASS (the `_normalize_echo` import of `surfacing_markers` is not hit by these tests; if a collection-time import error appears, create the stub `surfacing_markers.py` from Task 4 first — it is a two-line constant module.)

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/evolution.py tests/test_evolution_slot.py
git commit -m "feat(mind): evolution pending-slot schema + lifecycle state machine (evolution spec §3.4)"
```

---

### Task 4: shared pairwise-Jaccard helper + echo-equivalence + mechanical checks

**Files:**
- Create: `src/dollos/mind/surfacing_markers.py` (shared marker constants)
- Modify: `src/dollos/mind/persona_guard.py` (add `pairwise_jaccard`, after `response_drift_score`, line 157)
- Modify: `src/dollos/mind/evolution.py` (add `echo_equivalent`, `mechanical_checks`)
- Test: `tests/test_evolution_checks.py`

**Interfaces:**
- Consumes: `persona_guard._word_set` (existing), `persona_guard.pairwise_jaccard` (new), `check_persona_violations` (existing).
- Produces:
  - `persona_guard.pairwise_jaccard(text_a: str, text_b: str) -> float` — refactored Jaccard core shared with `response_drift_score`.
  - `evolution.echo_equivalent(text: str, reference: str) -> bool` — normalized-exact OR jieba-Jaccard ≥ `ECHO_SIMILARITY`.
  - `evolution.mechanical_checks(text: str, *, floor: int, cap: int, enforcement) -> str | None` — friendly failure reason, or None if clean.
  - `surfacing_markers.OLD / .NEW / .ALL` — marker constants.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_evolution_checks.py
"""Echo-equivalence + mechanical checks + shared pairwise Jaccard (spec §3.1/§3.3/§3.4)."""
from dollos.character import Enforcement
from dollos.mind import evolution as evo
from dollos.mind.persona_guard import pairwise_jaccard, response_drift_score


def test_pairwise_jaccard_identical_is_one():
    assert pairwise_jaccard("我喜歡監控數字", "我喜歡監控數字") == 1.0


def test_pairwise_jaccard_disjoint_is_zero():
    assert pairwise_jaccard("蘋果", "橘子") == 0.0


def test_pairwise_jaccard_both_empty_is_one():
    assert pairwise_jaccard("", "") == 1.0


def test_response_drift_score_still_works():
    # Refactor must not change existing behavior.
    assert response_drift_score("A", []) == 1.0


def test_echo_equivalent_exact_after_normalize():
    assert evo.echo_equivalent("  我 現在的樣子。 ", "我現在的樣子。") is True


def test_echo_equivalent_paraphrase_above_threshold():
    ref = "我現在監控數字跳動時會主動來勁,不再只是安靜待著。"
    para = "我現在監控數字跳動時會主動來勁,不再安靜待著。"
    assert evo.echo_equivalent(para, ref) is True


def test_echo_equivalent_genuinely_different_is_false():
    assert evo.echo_equivalent("我其實喜歡園藝跟做菜。", "我現在監控數字會來勁。") is False


def test_echo_equivalent_strips_surfacing_markers():
    from dollos.mind import surfacing_markers as sm
    surfaced = f"{sm.NEW} 我現在的樣子。"
    assert evo.echo_equivalent(surfaced, "我現在的樣子。") is True


def test_mechanical_checks_floor():
    reason = evo.mechanical_checks("太短", floor=80, cap=600, enforcement=Enforcement())
    assert reason is not None and "80" in reason


def test_mechanical_checks_cap():
    reason = evo.mechanical_checks("字" * 601, floor=80, cap=600, enforcement=Enforcement())
    assert reason is not None and "600" in reason


def test_mechanical_checks_banned_substring():
    enf = Enforcement(banned_substrings=["LARP"])
    reason = evo.mechanical_checks("我" * 80 + "LARP", floor=80, cap=600, enforcement=enf)
    assert reason is not None and "LARP" in reason


def test_mechanical_checks_clean_returns_none():
    assert evo.mechanical_checks("我" * 100, floor=80, cap=600, enforcement=Enforcement()) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_evolution_checks.py -v`
Expected: FAIL — `ImportError: cannot import name 'pairwise_jaccard'` (and missing `surfacing_markers`).

- [ ] **Step 3: Implement**

`src/dollos/mind/surfacing_markers.py` (NEW):

```python
# src/dollos/mind/surfacing_markers.py
"""Distinctive marker prefixes for the [人格演化候選] surfacing (spec §3.4).

Shared between the surfacing renderer (Task 8) and the echo-equivalence
normalizer (evolution._normalize_echo) so the two can never drift — an echoed
old/new block must strip exactly what the renderer prepended (A1
``_strip_incoming_tag`` analogue)."""
OLD = "【現行·舊】"
NEW = "【候選·新】"
ALL = (OLD, NEW)
```

`src/dollos/mind/persona_guard.py` — add after `response_drift_score` (line 157), and refactor `response_drift_score`'s inner Jaccard to share nothing new beyond `_word_set` (its union-vs-baselines semantics stay; the new helper is pairwise):

```python
def pairwise_jaccard(text_a: str, text_b: str) -> float:
    """Jaccard overlap of two single texts' normalized word-sets (jieba-
    segmented, spec §3.4/§3.5). 1.0 = identical vocabulary (or both empty);
    0.0 = disjoint. Shares the ``_word_set`` tokenizer with
    ``response_drift_score`` (which compares against a UNION of baselines — a
    different aggregation, deliberately kept separate). Pure, no I/O, no LLM."""
    wa = _word_set(text_a)
    wb = _word_set(text_b)
    union = wa | wb
    if not union:
        return 1.0
    return len(wa & wb) / len(union)
```

`src/dollos/mind/evolution.py` — append (after `_normalize_echo`):

```python
def echo_equivalent(text: str, reference: str) -> bool:
    """True when ``text`` is an echo/paraphrase of ``reference`` (spec §3.4):
    normalized-exact-equal OR jieba-Jaccard ≥ ``ECHO_SIMILARITY``. The weak
    model paraphrases; exact-match alone would misroute an intended adopt into a
    needless 送審 round-trip."""
    from dollos.mind.persona_guard import pairwise_jaccard
    n_text = _normalize_echo(text)
    n_ref = _normalize_echo(reference)
    if n_text == n_ref:
        return True
    return pairwise_jaccard(n_text, n_ref) >= ECHO_SIMILARITY


def mechanical_checks(text: str, *, floor: int, cap: int, enforcement) -> str | None:
    """Code-level gate applied to every origin's text at its entry point (spec
    §3.3): char floor/cap + ``check_persona_violations`` (banned substrings /
    exclaim runs). Returns a friendly failure reason, or None if clean. The
    echo-marker normalization is applied by ``echo_equivalent`` at the adopt
    site; length is measured on the raw candidate text."""
    from dollos.mind.persona_guard import check_persona_violations
    n = len(text)
    if n < floor:
        return f"太短了({n} 字,至少要 {floor} 字)——現在的我需要一段完整的描述。"
    if n > cap:
        return f"太長了({n} 字,上限 {cap} 字)——精簡一下。"
    violations = check_persona_violations(text, enforcement)
    if violations:
        return "牴觸人設約束:" + "、".join(violations)
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_evolution_checks.py tests/test_persona_guard.py -v`
Expected: all PASS (existing persona_guard tests untouched by the additive `pairwise_jaccard`).

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/surfacing_markers.py src/dollos/mind/persona_guard.py src/dollos/mind/evolution.py tests/test_evolution_checks.py
git commit -m "feat(mind): shared pairwise Jaccard + echo-equivalence + mechanical checks (evolution spec §3.3/§3.4)"
```

---

### Task 5: config `[evolution]` section

**Files:**
- Modify: `src/dollos/config.py` (add `EvolutionConfig` after `SelfProfileConfig` line 184; wire into `Settings` after `self_profile` line 204)
- Test: `tests/test_config_evolution.py`

**Interfaces:**
- Produces: `Settings.evolution: EvolutionConfig` with ALL §3.6 knobs (Plan 3 consumes the interval/material ones; defined once now to avoid churn).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config_evolution.py
"""[evolution] config section (spec §3.6)."""
import tomllib

from dollos.config import EvolutionConfig, Settings

_MIN = """
[llm]
base_url = "http://localhost:8001"
model_alias = "test"
[character]
pack = "packs/gura"
"""


def test_evolution_defaults():
    s = Settings.model_validate(tomllib.loads(_MIN))
    e = s.evolution
    assert e.enabled is True
    assert e.current_self_max_chars == 600
    assert e.current_self_min_chars == 80
    assert e.base_interval_days == 7.0
    assert e.max_interval_days == 28.0
    assert e.idle_threshold_s == 600
    assert e.min_history_events == 8
    assert e.min_diary_days == 14
    assert e.pending_max_surfacings == 5
    assert e.pending_min_age_days == 2.0


def test_evolution_override():
    toml = _MIN + '\n[evolution]\nenabled = false\ncurrent_self_max_chars = 400\n'
    s = Settings.model_validate(tomllib.loads(toml))
    assert s.evolution.enabled is False
    assert s.evolution.current_self_max_chars == 400


def test_evolution_rejects_unknown_key():
    import pytest
    from pydantic import ValidationError
    toml = _MIN + '\n[evolution]\nbogus = 1\n'
    with pytest.raises(ValidationError):
        Settings.model_validate(tomllib.loads(toml))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config_evolution.py -v`
Expected: FAIL — `ImportError: cannot import name 'EvolutionConfig'`

- [ ] **Step 3: Implement**

`src/dollos/config.py` — add after `SelfProfileConfig` (line 184):

```python
class EvolutionConfig(BaseModel):
    """慢變演化 — the slow, ratified 「現在的我」 personality prose (spec §3.6).

    ``enabled = false`` freezes the machinery (no trigger, no tool, no tripwire
    side-effects) but ALREADY-SANCTIONED text keeps rendering — disabling
    evolution must not amputate an adopted self (R3′). The interval/material
    knobs are consumed by Plan 3's keeper (Mode A); they are defined here once so
    Plan 3 adds no config churn.
    """
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    current_self_max_chars: int = 600
    current_self_min_chars: int = 80
    base_interval_days: float = 7.0    # floats — live smoke clamps to sub-day
    max_interval_days: float = 28.0
    idle_threshold_s: int = 600
    min_history_events: int = 8
    min_diary_days: int = 14
    pending_max_surfacings: int = 5
    pending_min_age_days: float = 2.0
```

Wire into `Settings` (after `self_profile` line 204):

```python
    evolution: EvolutionConfig = Field(default_factory=lambda: EvolutionConfig())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config_evolution.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/dollos/config.py tests/test_config_evolution.py
git commit -m "feat(config): [evolution] section with all §3.6 knobs (evolution spec §3.6)"
```

---

### Task 6: `SelfRevision` tool (pure decision logic against a stub ctx)

**Files:**
- Modify: `src/dollos/tools.py` (add `SelfRevision` after `PinSelf`, ~line 816; add `EVOLUTION_TOOLS` list after `KEEPER_TOOLS` line 837)
- Modify: `src/dollos/mind/mind_ctx.py` (add `evolution_latched`, `evolution_enabled`, `current_self_min_chars`, `current_self_max_chars`, `enforcement` fields after `external_ctx` line 57)
- Test: `tests/test_self_revision.py`

**Interfaces:**
- Consumes: `evolution.load_slot/save_slot/clear_slot/to_counter/echo_equivalent/mechanical_checks`, `self_history.log_event/sanctioned_text/latest_adopt`, `current_self.read_file`, `persona_guard.pairwise_jaccard`.
- Produces: `SelfRevision(decision: Literal["adopt","reject"], text: str = "", reason: str = "")` pydantic tool. `EVOLUTION_TOOLS: list[type[BaseModel]] = [SelfRevision]`.
- Requires MindCtx to carry: `evolution_latched: bool`, `evolution_enabled: bool`, `current_self_min_chars: int`, `current_self_max_chars: int`, `enforcement` (the pack's `Enforcement`).

**MindCtx additions** (after `external_ctx: bool = False`, line 57):

```python
    # 慢變演化 (spec 2026-07-02 §3.4): per-turn SelfRevision latch (reset at
    # drain by MindLoop) + static-per-run evolution config + pack enforcement.
    evolution_latched: bool = False
    evolution_enabled: bool = False
    current_self_min_chars: int = 80
    current_self_max_chars: int = 600
    enforcement: "Enforcement | None" = None
```

(Add `from dollos.character import Enforcement` under `TYPE_CHECKING` in `mind_ctx.py`.)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_self_revision.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_self_revision.py -v`
Expected: FAIL — `ImportError: cannot import name 'SelfRevision'`

- [ ] **Step 3: Implement**

`src/dollos/tools.py` — add after `PinSelf` (line 816). Note the grammar constraints (spec / `project_emoji_gating`): `Literal` enums must be ASCII; the `text`/`reason` field descriptions must ban fullwidth quotes (mirror PinSelf's `別用全形引號「」『』`).

```python
class SelfRevision(BaseModel):
    """採納 / 拒絕待批的「現在的我」演化候選(只在反思回合、且有待批候選時可用)。
    這是妳自己的人格描述,系統只提議,採不採納由妳。採納:decision="adopt",text 留空
    直接採納候選原文;想改寫後再採納:把完整新文字放進 text(會先送審,通過後回來給妳採
    納);不採納:decision="reject"。改寫只需不觸犯妳的核心身分與 taboos。"""

    decision: Literal["adopt", "reject"] = Field(
        description='"adopt"=採納(text 留空採納候選原文,或填全文改寫送審) / "reject"=不採納,維持現狀。'
    )
    text: str = Field(
        default="",
        description="想改寫後採納才填:完整替換文字(妳自己的話,別用全形引號「」『』);直接採納或拒絕時留空。",
    )
    reason: str = Field(
        default="",
        description="拒絕時可選填一句原因(妳自己的話,別用全形引號「」『』)。",
    )

    def _summary(self) -> str:
        return f"self-revision {self.decision}"

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind import current_self, evolution as evo, self_history
        from dollos.mind.persona_guard import pairwise_jaccard

        hist = ctx.memory_root / "self_history.jsonl"
        slot_path = ctx.memory_root / "self_evolution" / "pending.json"
        cs_path = ctx.memory_root / "current_self.md"

        # Per-turn latch (spec §3.4): first slot-mutating call per turn acts.
        if ctx.evolution_latched:
            _record(ctx, "SelfRevision", self._summary())
            return "這一輪已處理過人格演化候選了,下次反思再說。"

        slot = evo.load_slot(slot_path, history_path=hist)
        if slot is None:
            _record(ctx, "SelfRevision", self._summary())
            return "目前沒有待批的演化候選。"
        if slot.status != "awaiting_doll":
            _record(ctx, "SelfRevision", self._summary())
            return "候選還在送審中,通過後會回來給妳採納。"

        sanctioned = self_history.sanctioned_text(hist)

        def _restore_file_if_divergent() -> None:
            # Slot-resolution invariant (spec §3.4): any non-adopt clearing
            # restores the file to sanctioned (or deletes it, bootstrap).
            if current_self.read_file(cs_path) != (sanctioned or ""):
                if sanctioned is None:
                    try:
                        cs_path.unlink()
                    except FileNotFoundError:
                        pass
                    logger.warning("SelfRevision: restored current_self.md (deleted, bootstrap)")
                else:
                    _atomic_write_text(cs_path, sanctioned)
                    logger.warning("SelfRevision: restored current_self.md to sanctioned text")

        if self.decision == "reject":
            # Evolution events are never swallowed (spec §3.2): a failed append
            # aborts the reject — slot stays, no latch, friendly error.
            try:
                evo.log_or_raise(hist, kind=evo.EVO_REJECT, reason=self.reason or None,
                                 text=slot.candidate)
            except OSError:
                _record(ctx, "SelfRevision", self._summary())
                return "拒絕時寫記錄失敗了,先沒生效——稍後再試一次。"
            evo.clear_slot(slot_path)
            _restore_file_if_divergent()
            ctx.evolution_latched = True
            _record(ctx, "SelfRevision", self._summary())
            return "好,維持現狀,這個候選先擱著。"

        # decision == "adopt"
        proposed = self.text.strip()
        if not proposed or evo.echo_equivalent(proposed, slot.candidate):
            # Adopt the CANDIDATE verbatim (never her paraphrase, spec §3.4).
            old = sanctioned
            drift = None if old is None else round(1.0 - pairwise_jaccard(old, slot.candidate), 4)
            # Log-then-write ordering (spec §3.2): a failed append aborts.
            try:
                evo.log_or_raise(hist, kind=evo.EVO_ADOPT, text=slot.candidate,
                                 old_text=old, drift_score=drift)
            except OSError:
                _record(ctx, "SelfRevision", self._summary())
                return "採納時寫記錄失敗了,先沒生效——稍後再試一次。"
            _atomic_write_text(cs_path, slot.candidate)
            evo.clear_slot(slot_path)
            ctx.evolution_latched = True
            _record(ctx, "SelfRevision", self._summary())
            return "採納了,這就是現在的我。"

        if sanctioned is not None and evo.echo_equivalent(proposed, sanctioned):
            _record(ctx, "SelfRevision", self._summary())
            return "這和現在的內容其實一樣;要維持現狀就用 decision=reject。"

        # Genuinely different → counter-proposal.
        if slot.counter_round >= evo.COUNTER_ROUND_CAP:
            _record(ctx, "SelfRevision", self._summary())
            return "這個候選已經改寫過兩次了,請直接採納或拒絕。"
        reason = evo.mechanical_checks(
            proposed, floor=ctx.current_self_min_chars,
            cap=ctx.current_self_max_chars, enforcement=ctx.enforcement)
        if reason is not None:
            _record(ctx, "SelfRevision", self._summary())
            return reason
        counter = evo.to_counter(slot, new_text=proposed, created_ts_now=time.time())
        # Birth-line-then-write (spec §3.4: every slot has a birth line): log
        # the evo_counter BEFORE replacing the slot; a failed append aborts
        # with the slot unchanged (spec §3.2 — never swallowed).
        try:
            evo.log_or_raise(hist, kind=evo.EVO_COUNTER, text=proposed,
                             counter_round=counter.counter_round)
        except OSError:
            _record(ctx, "SelfRevision", self._summary())
            return "送審時寫記錄失敗了,先沒生效——稍後再試一次。"
        evo.save_slot(slot_path, counter)
        ctx.evolution_latched = True
        _record(ctx, "SelfRevision", self._summary())
        return "你的改寫已送審,通過後會回來給你採納。"
```

Add two small helpers near the top of `tools.py` (after `_record`, line 46):

```python
def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic tmp+rename write (sanctioned-writer discipline, spec §3.1)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
```

And in `src/dollos/mind/evolution.py`, add the evolution-event log wrapper (evolution events ABORT on IO error — the opposite of the pins-only swallow, spec §3.2):

```python
def log_or_raise(history_path: Path, *, kind: str, **fields) -> None:
    """Append an evolution event; RAISES OSError on IO failure (spec §3.2 —
    evolution events are never swallowed, unlike pin events). ``None`` field
    values are kept (e.g. first-adoption ``old_text=None``/``drift_score=None``)."""
    from dollos.mind import self_history
    self_history.log_event(history_path, kind=kind, **fields)
```

Register the tool list (`tools.py`, after `KEEPER_TOOLS` line 837):

```python
# 慢變演化 (spec §3.4): reflection-turn tool, gated on evolution.enabled.
EVOLUTION_TOOLS: list[type[BaseModel]] = [SelfRevision]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_self_revision.py -v`
Expected: 13 PASS

- [ ] **Step 5: Commit**

```bash
git add src/dollos/tools.py src/dollos/mind/mind_ctx.py src/dollos/mind/evolution.py tests/test_self_revision.py
git commit -m "feat(tools): SelfRevision adopt/reject/counter with per-turn latch (evolution spec §3.4)"
```

---

### Task 7: composition seam — scaffolding split + kernel + MindLoop per-turn compose

**Files:**
- Modify: `src/dollos/prompts/templates/scaffolding.jinja` (replace blank line 14 with the seam variable)
- Modify: `src/dollos/kernel.py` (render with sentinel + split ~line 285; pass suffix + evolution config to MindLoop ~line 307; MindCtx fields ~line 264)
- Modify: `src/dollos/mind/mind_loop.py` (constructor + `_system_prompt_for_turn` + render call site line 285)
- Test: `tests/test_composition_seam.py`

**Interfaces:**
- Consumes: `current_self.compose/render_section`, `self_history.sanctioned_text`.
- Produces: `MindLoop.__init__(..., system_prompt_suffix: str = "", evolution_enabled: bool = False, current_self_min_chars: int = 80, current_self_max_chars: int = 600)`; `MindLoop._system_prompt_for_turn() -> str` with a content-keyed cache. Kernel `_CURRENT_SELF_SEAM` sentinel constant.

**scaffolding.jinja** — replace the blank line 14 (between `{%- endif %}` and `# Behavior`) with:

```jinja
{{ current_self_seam }}
```

(jinja2's default `Undefined` renders to `""`, so existing callers that don't pass `current_self_seam` — every renderer test — are byte-neutral: the line renders empty, exactly like the blank line it replaced.)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_composition_seam.py
"""Three-piece system-prompt composition seam (spec §3.1)."""
from dollos.character import Identity
from dollos.kernel import _CURRENT_SELF_SEAM, split_scaffolding
from dollos.prompts import PromptRenderer


def _identity():
    return Identity(self="You are Gura.", personality="- chill", taboos="- no LARP")


def test_split_reconstructs_today_when_no_section():
    r = PromptRenderer()
    prefix, suffix = split_scaffolding(r, identity=_identity(), available_skills=[], tool_registry={})
    baseline = r.render("scaffolding", identity=_identity(), available_skills=[], tool_registry={})
    # Empty section ⇒ prefix + suffix is byte-identical to today's render.
    assert prefix + suffix == baseline
    assert _CURRENT_SELF_SEAM not in (prefix + suffix)


def test_split_seam_between_taboos_and_behavior():
    r = PromptRenderer()
    prefix, suffix = split_scaffolding(r, identity=_identity(), available_skills=[], tool_registry={})
    assert "no LARP" in prefix
    assert "# Behavior" in suffix


def test_mindloop_compose_renders_section_when_sanctioned(tmp_path):
    from dollos.mind import self_history
    self_history.log_event(tmp_path / "self_history.jsonl", kind="evo_adopt",
                           text="我現在監控數字時會主動來勁。", old_text=None, drift_score=None)
    from tests._mindloop_factory import make_mindloop  # see note below
    ml = make_mindloop(memory_root=tmp_path, system_prompt="PFX\n",
                       system_prompt_suffix="\n# Behavior\n")
    out = ml._system_prompt_for_turn()
    assert "## 現在的我" in out and "監控數字" in out


def test_mindloop_compose_omits_section_when_no_sanctioned(tmp_path):
    from tests._mindloop_factory import make_mindloop
    ml = make_mindloop(memory_root=tmp_path, system_prompt="PFX\n",
                       system_prompt_suffix="\n# Behavior\n")
    assert ml._system_prompt_for_turn() == "PFX\n\n# Behavior\n"


def test_mindloop_compose_cache_keyed_on_sanctioned(tmp_path):
    from dollos.mind import self_history
    from tests._mindloop_factory import make_mindloop
    ml = make_mindloop(memory_root=tmp_path, system_prompt="PFX\n",
                       system_prompt_suffix="\n# Behavior\n")
    first = ml._system_prompt_for_turn()
    second = ml._system_prompt_for_turn()
    assert first is second  # cached object identity (no recompose)
    self_history.log_event(tmp_path / "self_history.jsonl", kind="evo_adopt",
                           text="換了一版的我。", old_text=None, drift_score=None)
    third = ml._system_prompt_for_turn()
    assert third is not first and "換了一版" in third
```

Note: `tests/_mindloop_factory.py` is a tiny shared helper that constructs a `MindLoop` with stub dependencies (queue/llm/ctx). Check whether an existing test module already builds a `MindLoop` stub (`grep -rn "MindLoop(" tests/`) and reuse that constructor pattern verbatim; only extract the shared factory if no such helper exists. The factory must set `ctx.memory_root` to the passed `memory_root`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_composition_seam.py -v`
Expected: FAIL — `ImportError: cannot import name '_CURRENT_SELF_SEAM'`

- [ ] **Step 3: Implement**

`src/dollos/kernel.py` — module level (after imports, ~line 57):

```python
# Sentinel for the three-piece system-prompt seam (spec §3.1). Rendered into
# scaffolding.jinja's seam line, then split out — chosen so it can never occur
# in natural prose or pack content.
_CURRENT_SELF_SEAM = "\x00\x00DOLLOS_CURRENT_SELF_SEAM\x00\x00"


def split_scaffolding(renderer, **ctx) -> tuple[str, str]:
    """Render scaffolding with the seam sentinel and split into (prefix,
    suffix). ``prefix + suffix`` is byte-identical to a seam-less render, so a
    run with no sanctioned ``current_self`` reproduces today's prompt exactly
    (spec §3.1)."""
    rendered = renderer.render("scaffolding", current_self_seam=_CURRENT_SELF_SEAM, **ctx)
    prefix, _, suffix = rendered.partition(_CURRENT_SELF_SEAM)
    return prefix, suffix
```

In `DollOS.__init__`, replace the `system_prompt = self.renderer.render("scaffolding", ...)` block (lines 285-290) with:

```python
        system_prompt_prefix, system_prompt_suffix = split_scaffolding(
            self.renderer,
            identity=self._doll_pack.identity,
            available_skills=available_skills,
            tool_registry=tool_registry,
        )
```

Add the evolution fields to `MindCtx(...)` construction (after `self_profile_max_chars=...` line 274):

```python
            evolution_enabled=settings.evolution.enabled,
            current_self_min_chars=settings.evolution.current_self_min_chars,
            current_self_max_chars=settings.evolution.current_self_max_chars,
            enforcement=self._doll_pack.enforcement,
```

Update `MindLoop(...)` construction (lines 307-324): change `system_prompt=system_prompt,` to `system_prompt=system_prompt_prefix,` and add:

```python
            system_prompt_suffix=system_prompt_suffix,
            evolution_enabled=settings.evolution.enabled,
            current_self_min_chars=settings.evolution.current_self_min_chars,
            current_self_max_chars=settings.evolution.current_self_max_chars,
```

`src/dollos/mind/mind_loop.py` — constructor: add params after `enforcement` (line 124) and store them + init the cache:

```python
        system_prompt_suffix: str = "",
        evolution_enabled: bool = False,
        current_self_min_chars: int = 80,
        current_self_max_chars: int = 600,
```
```python
        self._system_prompt_suffix = system_prompt_suffix
        self._evolution_enabled = evolution_enabled
        self._current_self_min_chars = current_self_min_chars
        self._current_self_max_chars = current_self_max_chars
        # Content-keyed compose cache: (sanctioned_text_or_"", composed_prompt).
        # Recompose only when sanctioned text changes (weeks) — the prompt
        # cache stays warm (spec §3.1).
        self._composed_cache: tuple[str, str] | None = None
```

Add the compose method (after `__init__`, before `run`):

```python
    def _system_prompt_for_turn(self) -> str:
        """Compose ``prefix ⊕ current_self_section ⊕ suffix`` for this turn,
        reading the SANCTIONED text (spec §3.1/§5). Content-keyed cache: the
        section changes only when the latest ``evo_adopt`` changes. Sanctioned
        text renders even when ``evolution.enabled`` is false — disabling
        evolution must not amputate an adopted self (spec §3.6)."""
        from dollos.mind import current_self, self_history
        sanctioned = self_history.sanctioned_text(
            self._ctx.memory_root / "self_history.jsonl"
        )
        key = sanctioned or ""
        if self._composed_cache is None or self._composed_cache[0] != key:
            section = current_self.render_section(sanctioned)
            composed = current_self.compose(
                self._system_prompt, section, self._system_prompt_suffix
            )
            self._composed_cache = (key, composed)
        return self._composed_cache[1]
```

In `iterate()`, change the `render_mind(self._state, memsearch_hits, self._system_prompt, ...)` call (line 285-297) to pass the composed prompt:

```python
            prompt = render_mind(
                self._state,
                memsearch_hits,
                self._system_prompt_for_turn(),
                ...
```

(leave every other kwarg unchanged.)

- [ ] **Step 4: Run tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_composition_seam.py tests/test_prompt_renderer.py -v`
Expected: all PASS (renderer tests unchanged — seam renders empty).
Run: `uv run pytest` — the FULL suite, not just this task's tests.
Expected: all green (no existing MindLoop test regressed — with no sanctioned text, the composed prompt equals `prefix + suffix`; kernel-built prompts are cosmetically identical to today).
Note (`evolution.enabled` defaults True): any daemon-level test that snapshots the rendered system prompt or golden prompts may see the split-render path now. If one breaks, adjudicate: byte-equality with today's render is the CONTRACT for the no-sanctioned-text case (`test_split_reconstructs_today_when_no_section`) — a golden-prompt diff here is a real bug, not a snapshot to update.

- [ ] **Step 5: Commit**

```bash
git add src/dollos/prompts/templates/scaffolding.jinja src/dollos/kernel.py src/dollos/mind/mind_loop.py src/dollos/mind/mind_ctx.py tests/test_composition_seam.py tests/_mindloop_factory.py
git commit -m "feat(mind): three-piece system-prompt composition seam (evolution spec §3.1)"
```

---

### Task 8: surfacing perception + reflection wiring + SelfRevision registry/grammar + latch reset

**Files:**
- Modify: `src/dollos/mind/evolution.py` (add `render_surfacing` + `surface_or_expire` orchestrator)
- Modify: `src/dollos/mind/mind_prompt.py` (add `evolution_block` param to `render_mind`)
- Modify: `src/dollos/mind/mind_loop.py` (compute block on reflection turns; `_active_tool_registry`/`_active_grammar` add `SelfRevision`; reset `evolution_latched` at drain)
- Test: `tests/test_evolution_surfacing.py`, `tests/test_mind_loop_evolution_wiring.py`

**Interfaces:**
- Produces:
  - `evolution.render_surfacing(*, slot: PendingSlot, sanctioned_text: str | None, reminder_n: int) -> str` — the `[人格演化候選]` block (marker-prefixed old/new full text, per-origin note, operational hint, 主權句, 第N次提醒).
  - `evolution.surface_or_expire(*, slot_path, history_path, current_self_path, sanctioned_text, max_surfacings: int, min_age_days: float, now: float) -> str | None` — returns the surfacing block (and increments `surfaced_count`), OR expires the slot (`evo_expire` + slot-resolution restore) and returns None.
- `render_mind(..., evolution_block: str | None = None)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_evolution_surfacing.py
"""[人格演化候選] surfacing + expiry (spec §3.4)."""
from dollos.mind import evolution as evo
from dollos.mind import self_history


def test_render_surfacing_keeper_has_all_load_bearing_parts():
    slot = evo.make_keeper_slot(candidate="我現在監控數字時會主動來勁。", rationale="活很久的 pin",
                                hwm_before=None, created_ts=1.0)
    out = evo.render_surfacing(slot=slot, sanctioned_text="我以前沒事就安靜待著。", reminder_n=1)
    assert "[人格演化候選]" in out
    assert "我以前沒事就安靜待著。" in out and "我現在監控數字時會主動來勁。" in out
    assert "活很久的 pin" in out            # rationale (keeper)
    assert "adopt" in out and "reject" in out  # operational hint
    assert "採不採納由妳" in out or "由妳" in out  # 主權句
    assert "第 1 次" in out                  # 第N次提醒


def test_render_surfacing_external_uses_neutral_attribution():
    slot = evo.make_external_slot(candidate="有人手動改的內容。", created_ts=1.0)
    slot.status = "awaiting_doll"
    out = evo.render_surfacing(slot=slot, sanctioned_text=None, reminder_n=2)
    # Neutral attribution — never "可能是主人" (spec §3.4).
    assert "無法確認是誰" in out
    assert "可能是主人" not in out


def test_render_surfacing_counter_kill_notice_leads():
    base = evo.make_keeper_slot(candidate="原候選內容。", rationale="R", hwm_before=None, created_ts=1.0)
    c = evo.to_counter(base, new_text="我的改寫。", created_ts_now=2.0)
    reverted = evo.revert_to_fallback(c, reason="牴觸 taboo")
    out = evo.render_surfacing(slot=reverted, sanctioned_text=None, reminder_n=1)
    assert "未通過" in out and "牴觸 taboo" in out and "原候選內容。" in out


def test_surface_increments_count(tmp_path):
    sp = tmp_path / "self_evolution" / "pending.json"
    evo.save_slot(sp, evo.make_keeper_slot(candidate="x"*90, rationale="R",
                                           hwm_before=None, created_ts=0.0))
    block = evo.surface_or_expire(
        slot_path=sp, history_path=tmp_path / "self_history.jsonl",
        current_self_path=tmp_path / "current_self.md", sanctioned_text=None,
        max_surfacings=5, min_age_days=2.0, now=1.0)
    assert block is not None
    assert evo.load_slot(sp).surfaced_count == 1


def test_surface_awaiting_skeptic_returns_none(tmp_path):
    sp = tmp_path / "self_evolution" / "pending.json"
    evo.save_slot(sp, evo.make_external_slot(candidate="x"*90, created_ts=0.0))
    block = evo.surface_or_expire(
        slot_path=sp, history_path=tmp_path / "self_history.jsonl",
        current_self_path=tmp_path / "current_self.md", sanctioned_text=None,
        max_surfacings=5, min_age_days=2.0, now=1.0)
    assert block is None
    assert evo.load_slot(sp).surfaced_count == 0  # awaiting_skeptic never increments


def test_expiry_needs_count_and_age(tmp_path):
    import time as _t
    sp = tmp_path / "self_evolution" / "pending.json"
    hist = tmp_path / "self_history.jsonl"
    slot = evo.make_keeper_slot(candidate="x"*90, rationale="R", hwm_before=None, created_ts=0.0)
    slot.surfaced_count = 5  # count threshold met
    evo.save_slot(sp, slot)
    day = 86400.0
    # Age NOT met (now < min_age_days) → still surfaces, not expired.
    block = evo.surface_or_expire(slot_path=sp, history_path=hist,
                                  current_self_path=tmp_path/"current_self.md",
                                  sanctioned_text=None, max_surfacings=5,
                                  min_age_days=2.0, now=1.0)
    assert block is not None and evo.load_slot(sp) is not None
    # Age met too → expires.
    block = evo.surface_or_expire(slot_path=sp, history_path=hist,
                                  current_self_path=tmp_path/"current_self.md",
                                  sanctioned_text=None, max_surfacings=5,
                                  min_age_days=2.0, now=3 * day)
    assert block is None
    assert evo.load_slot(sp) is None
    assert self_history.read_events(hist)[-1]["kind"] == "evo_expire"


def test_expiry_restores_divergent_file(tmp_path):
    sp = tmp_path / "self_evolution" / "pending.json"
    hist = tmp_path / "self_history.jsonl"
    cs = tmp_path / "current_self.md"
    cs.write_text("有人手動改的", encoding="utf-8")
    slot = evo.make_external_slot(candidate="有人手動改的", created_ts=0.0)
    slot.status = "awaiting_doll"
    slot.surfaced_count = 5
    evo.save_slot(sp, slot)
    evo.surface_or_expire(slot_path=sp, history_path=hist, current_self_path=cs,
                          sanctioned_text=None, max_surfacings=5, min_age_days=0.0,
                          now=1.0)
    assert not cs.exists()  # slot-resolution invariant: bootstrap restore = delete
```

```python
# tests/test_mind_loop_evolution_wiring.py
"""MindLoop wires SelfRevision into the reflection registry/grammar + latch reset."""
from dollos.tools import SelfRevision


def test_reflection_registry_includes_self_revision_when_enabled(tmp_path):
    from tests._mindloop_factory import make_mindloop
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True)
    ml._is_reflection = True
    ml._state.safe_mode = False
    assert "SelfRevision" in ml._active_tool_registry()


def test_reflection_registry_excludes_self_revision_when_disabled(tmp_path):
    from tests._mindloop_factory import make_mindloop
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=False)
    ml._is_reflection = True
    ml._state.safe_mode = False
    assert "SelfRevision" not in ml._active_tool_registry()


def test_safe_mode_excludes_self_revision(tmp_path):
    from tests._mindloop_factory import make_mindloop
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True)
    ml._is_reflection = True
    ml._state.safe_mode = True
    assert "SelfRevision" not in ml._active_tool_registry()


def test_self_revision_in_refeed_allowlist():
    from dollos.mind.mind_loop import IN_TURN_REFEED_TOOLS
    assert "SelfRevision" in IN_TURN_REFEED_TOOLS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_evolution_surfacing.py tests/test_mind_loop_evolution_wiring.py -v`
Expected: FAIL — `AttributeError: module 'dollos.mind.evolution' has no attribute 'render_surfacing'`

- [ ] **Step 3: Implement**

`src/dollos/mind/evolution.py` — append:

```python
def render_surfacing(*, slot: PendingSlot, sanctioned_text: str | None,
                     reminder_n: int) -> str:
    """The ``[人格演化候選]`` perception body shown on ``awaiting_doll`` reflection
    turns (spec §3.4). Marker-prefixed old/new full text + per-origin note +
    operational hint + 主權句 + 第N次提醒 (the reminder count breaks byte-
    identical correlated failure across surfacings)."""
    from dollos.mind import surfacing_markers as sm
    old = sanctioned_text if sanctioned_text else "(還沒有現在的我——這會是第一版)"
    lines = [
        "[人格演化候選]",
        f"（第 {reminder_n} 次提醒)",
        f"{sm.OLD} {old}",
        f"{sm.NEW} {slot.candidate}",
    ]
    if slot.notice:
        lines.insert(1, f"你上一次的改寫未通過({slot.notice})——原候選仍在,如下。")
    if slot.kind == "keeper" and slot.rationale:
        lines.append(f"依據:{slot.rationale}")
    elif slot.kind == "counter":
        lines.append("來源:你自己的改寫,已通過送審。")
    elif slot.kind == "external":
        lines.append("來源:current_self.md 檔案被直接修改,系統無法確認是誰。")
    lines.append(
        "採納:SelfRevision decision=adopt(不必填 text);"
        "不採納:decision=reject;"
        "想改寫後採納:把全文放進 text,會先送審再回來。"
    )
    lines.append("這是妳的人格描述——採不採納由妳;改寫只需不觸犯妳的核心身分與 taboos。")
    return "\n".join(lines)


def surface_or_expire(*, slot_path: Path, history_path: Path,
                      current_self_path: Path, sanctioned_text: str | None,
                      max_surfacings: int, min_age_days: float,
                      now: float) -> str | None:
    """On a reflection turn: surface an ``awaiting_doll`` slot (incrementing
    ``surfaced_count``, clearing a one-shot ``notice``), OR expire it when
    ``surfaced_count ≥ max_surfacings`` AND age ≥ ``min_age_days`` (spec §3.4).
    Expiry logs ``evo_expire`` loud, clears the slot, and restores the file per
    the slot-resolution invariant. Returns the block, or None (no slot /
    awaiting_skeptic / just expired)."""
    slot = load_slot(slot_path, history_path=history_path)
    if slot is None or slot.status != "awaiting_doll":
        return None

    age_days = (now - slot.created_ts) / 86400.0
    if slot.surfaced_count >= max_surfacings and age_days >= min_age_days:
        logger.warning("evolution: expiring pending slot (kind=%s, surfaced=%d)",
                       slot.kind, slot.surfaced_count)
        log_or_raise(history_path, kind=EVO_EXPIRE, text=slot.candidate,
                     kind_origin=slot.kind, hwm_before=slot.hwm_before)
        clear_slot(slot_path)
        _restore_file(current_self_path, sanctioned_text)
        return None

    reminder_n = slot.surfaced_count + 1
    block = render_surfacing(slot=slot, sanctioned_text=sanctioned_text,
                             reminder_n=reminder_n)
    slot.surfaced_count = reminder_n
    slot.notice = None  # one-shot: cleared after its first surfacing
    save_slot(slot_path, slot)
    return block


def _restore_file(current_self_path: Path, sanctioned_text: str | None) -> None:
    """Slot-resolution invariant (spec §3.4): restore the file to sanctioned
    text if divergent, or delete it in the bootstrap (no-sanctioned) case.
    Logged loudly — a silently self-reverting file reads as the daemon fighting
    its owner."""
    from dollos.mind import current_self
    current = current_self.read_file(current_self_path)
    if current == (sanctioned_text or ""):
        return
    if sanctioned_text is None:
        try:
            current_self_path.unlink()
        except FileNotFoundError:
            pass
        logger.warning("evolution: restored current_self.md (deleted, bootstrap)")
    else:
        tmp = current_self_path.with_suffix(current_self_path.suffix + ".tmp")
        tmp.write_text(sanctioned_text, encoding="utf-8")
        tmp.replace(current_self_path)
        logger.warning("evolution: restored current_self.md to sanctioned text")
```

`src/dollos/mind/mind_prompt.py` — add `evolution_block: str | None = None` to `render_mind`'s signature (after `self_profile_text`, line 50), and insert it near the top of the blocks (right after the `[Self profile]` block, before `[Memory guideline]`) so it is salient on reflection turns:

```python
    if evolution_block:
        blocks.extend([evolution_block, ""])
```

`src/dollos/mind/mind_loop.py`:

1. Reset the latch at drain — in `iterate()`, right after `self._ctx.external_ctx = batch_external(perceptions)` (line 207):

```python
        # 慢變演化 per-turn latch reset (spec §3.4): a new perception batch
        # opens a fresh SelfRevision decision window.
        self._ctx.evolution_latched = False
```

2. Compute the surfacing block on reflection turns — in the `try:` render block, after `self_profile_text` is computed (after line 284), before the `render_mind(...)` call:

```python
            evolution_block = None
            if (self._evolution_enabled and self._is_reflection
                    and not self._state.safe_mode):
                from dollos.mind import evolution as _evo, self_history as _sh
                import time as _time
                hist_path = self._ctx.memory_root / "self_history.jsonl"
                evolution_block = _evo.surface_or_expire(
                    slot_path=self._ctx.memory_root / "self_evolution" / "pending.json",
                    history_path=hist_path,
                    current_self_path=self._ctx.memory_root / "current_self.md",
                    sanctioned_text=_sh.sanctioned_text(hist_path),
                    max_surfacings=self._pending_max_surfacings,
                    min_age_days=self._pending_min_age_days,
                    now=_time.time(),
                )
```

and pass `evolution_block=evolution_block` into `render_mind(...)`.

3. Store the two surfacing config values in the constructor (they come from `settings.evolution`, threaded via kernel Task 7 — add `pending_max_surfacings: int = 5, pending_min_age_days: float = 2.0` params and `self._pending_max_surfacings = ...`). Update the kernel `MindLoop(...)` construction to pass `pending_max_surfacings=settings.evolution.pending_max_surfacings, pending_min_age_days=settings.evolution.pending_min_age_days`.

4. `_active_tool_registry` (line 433 reflection branch): add `SelfRevision` when `self._evolution_enabled` (mirror the `PinSelf` conditional):

```python
        if self._is_reflection:
            extra = {"NoteToolLesson": NoteToolLesson}
            if self._self_profile_enabled:
                extra["PinSelf"] = PinSelf
            if self._evolution_enabled:
                from dollos.tools import SelfRevision
                extra["SelfRevision"] = SelfRevision
            return {**self._tool_registry, **extra}
```

The reflection grammar (`_active_grammar`, line 455) builds from `_active_tool_registry().values()` and is cached once — since `_evolution_enabled` is static per run, `SelfRevision` is automatically in the cached reflection grammar. Safe mode returns the read-only subset first, so `SelfRevision` is excluded there (grammar-level suppression, spec §3.4).

5. Add `SelfRevision` to the in-turn re-feed allowlist (spec §3.4: "in `IN_TURN_REFEED_TOOLS`; idempotency = the per-turn latch"). `mind_loop.py` line 66:

```python
IN_TURN_REFEED_TOOLS = frozenset({"Recall", "PinSelf", "SelfRevision"})
```

so a `SelfRevision` success (e.g. the counter「已送審」/refusal message) re-feeds and Doll can read the outcome; a second mutation the same turn is defanged by the per-turn latch, not by exclusion.

- [ ] **Step 4: Run tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_evolution_surfacing.py tests/test_mind_loop_evolution_wiring.py tests/test_mind_prompt.py -v`
Expected: all PASS
Run: `uv run pytest` — the FULL suite, not just this task's tests.
Expected: all green.
Note (`evolution.enabled` defaults True): existing tests that snapshot the reflection tool registry, the reflection grammar, or the `IN_TURN_REFEED_TOOLS` set may now see `SelfRevision`. If any break, adjudicate: a snapshot that merely enumerates the reflection surface should be UPDATED to include `SelfRevision` (correct new behavior); a failure implying `SelfRevision` leaks into the base registry, safe-mode set, or non-reflection grammar is a real bug — fix the wiring, not the test.

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/evolution.py src/dollos/mind/mind_prompt.py src/dollos/mind/mind_loop.py src/dollos/kernel.py tests/test_evolution_surfacing.py tests/test_mind_loop_evolution_wiring.py
git commit -m "feat(mind): [人格演化候選] surfacing + SelfRevision reflection wiring + latch reset (evolution spec §3.4)"
```

---

### Task 9: tripwire orchestrator + wiring into the render path

**Files:**
- Modify: `src/dollos/mind/evolution.py` (add `process_tripwire` orchestrator)
- Modify: `src/dollos/mind/mind_loop.py` (call `process_tripwire` per turn before composing, when `evolution_enabled`)
- Test: `tests/test_evolution_tripwire.py`

**Interfaces:**
- Produces: `evolution.process_tripwire(*, current_self_path, history_path, slot_path, enforcement, floor, cap, now) -> None` — runs the §5 transition-gated tripwire (crash-repair / new-edit / already-logged / in-sync) using `current_self.classify_tripwire` + `self_history` reads.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_evolution_tripwire.py
"""Tamper tripwire orchestrator (spec §5)."""
from dollos.character import Enforcement
from dollos.mind import evolution as evo
from dollos.mind import self_history


def _tp(tmp_path, **kw):
    defaults = dict(
        current_self_path=tmp_path / "current_self.md",
        history_path=tmp_path / "self_history.jsonl",
        slot_path=tmp_path / "self_evolution" / "pending.json",
        enforcement=Enforcement(), floor=80, cap=600, now=1.0)
    defaults.update(kw)
    return evo.process_tripwire(**defaults)


def _kinds(tmp_path):
    return [e["kind"] for e in self_history.read_events(tmp_path / "self_history.jsonl")]


def test_in_sync_no_side_effects(tmp_path):
    # sanctioned == file → nothing.
    self_history.log_event(tmp_path/"self_history.jsonl", kind="evo_adopt",
                           text="我"*90, old_text=None, drift_score=None)
    (tmp_path/"current_self.md").write_text("我"*90, encoding="utf-8")
    _tp(tmp_path)
    assert _kinds(tmp_path) == ["evo_adopt"]


def test_crash_repair_rewrites_file_no_slot(tmp_path):
    hist = tmp_path/"self_history.jsonl"
    self_history.log_event(hist, kind="evo_adopt", text="新版"+"字"*88,
                           old_text="舊版"+"字"*88, drift_score=0.1)
    cs = tmp_path/"current_self.md"
    cs.write_text("舊版"+"字"*88, encoding="utf-8")  # == old_text (log-then-write window)
    _tp(tmp_path)
    assert cs.read_text(encoding="utf-8") == "新版"+"字"*88
    assert _kinds(tmp_path)[-1] == "evo_repair"
    assert not (tmp_path/"self_evolution"/"pending.json").exists()


def test_new_edit_passing_checks_creates_external_slot(tmp_path):
    self_history.log_event(tmp_path/"self_history.jsonl", kind="evo_adopt",
                           text="原版"+"字"*88, old_text=None, drift_score=None)
    (tmp_path/"current_self.md").write_text("有人改成這樣"+"字"*88, encoding="utf-8")
    _tp(tmp_path)
    assert _kinds(tmp_path)[-1] == "external_edit"
    slot = evo.load_slot(tmp_path/"self_evolution"/"pending.json")
    assert slot.kind == "external" and slot.status == "awaiting_skeptic"


def test_new_edit_failing_checks_restores_and_no_slot(tmp_path):
    self_history.log_event(tmp_path/"self_history.jsonl", kind="evo_adopt",
                           text="原版"+"字"*88, old_text=None, drift_score=None)
    cs = tmp_path/"current_self.md"
    cs.write_text("太短", encoding="utf-8")  # fails floor
    _tp(tmp_path)
    edits = [e for e in self_history.read_events(tmp_path/"self_history.jsonl")
             if e["kind"] == "external_edit"]
    assert edits[-1]["reason"] is not None  # mechanical-fail carries a reason
    assert cs.read_text(encoding="utf-8") == "原版"+"字"*88  # restored
    assert not (tmp_path/"self_evolution"/"pending.json").exists()


def test_edit_while_slot_exists_logs_only(tmp_path):
    self_history.log_event(tmp_path/"self_history.jsonl", kind="evo_adopt",
                           text="原版"+"字"*88, old_text=None, drift_score=None)
    evo.save_slot(tmp_path/"self_evolution"/"pending.json",
                  evo.make_external_slot(candidate="別的候選"+"字"*88, created_ts=0.0))
    (tmp_path/"current_self.md").write_text("又有人改"+"字"*88, encoding="utf-8")
    _tp(tmp_path)
    assert _kinds(tmp_path)[-1] == "external_edit"
    # Slot NOT replaced — external edits are not queued for auto-promotion.
    assert evo.load_slot(tmp_path/"self_evolution"/"pending.json").candidate == "別的候選"+"字"*88


def test_unchanged_divergent_no_spam(tmp_path):
    hist = tmp_path/"self_history.jsonl"
    self_history.log_event(hist, kind="evo_adopt", text="原版"+"字"*88,
                           old_text=None, drift_score=None)
    (tmp_path/"current_self.md").write_text("有人改"+"字"*88, encoding="utf-8")
    _tp(tmp_path)  # first detection → external_edit + slot
    n1 = len(self_history.read_events(hist))
    _tp(tmp_path)  # second turn, same file → no new log
    assert len(self_history.read_events(hist)) == n1


def test_bootstrap_new_edit_restore_is_delete(tmp_path):
    cs = tmp_path/"current_self.md"
    cs.write_text("太短", encoding="utf-8")  # no sanctioned predecessor, fails floor
    _tp(tmp_path)
    assert not cs.exists()  # bootstrap restore = delete
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_evolution_tripwire.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'process_tripwire'`

- [ ] **Step 3: Implement**

`src/dollos/mind/evolution.py` — append:

```python
def process_tripwire(*, current_self_path: Path, history_path: Path,
                     slot_path: Path, enforcement, floor: int, cap: int,
                     now: float) -> None:
    """Transition-gated tamper tripwire (spec §5). Runs at render time when
    ``evolution.enabled``. Never writes unratified bytes into the identity
    region (composition always renders SANCTIONED text); this only detects
    edits, repairs the log-then-write window, and creates external slots.

    - in_sync        → nothing.
    - crash_repair   → rewrite file to sanctioned, log ``evo_repair``; no slot.
    - already_logged → nothing (no per-turn spam).
    - new_edit       → append ONE ``external_edit``; mechanical checks; fail →
                       restore/delete + ``external_edit(reason)``; pass → create
                       an external ``awaiting_skeptic`` slot IFF none exists
                       (else logs-only — external edits are not auto-promoted)."""
    from dollos.mind import current_self, self_history

    file_text = current_self.read_file(current_self_path)
    sanctioned = self_history.sanctioned_text(history_path)
    adopt = self_history.latest_adopt(history_path)
    adopt_old = adopt.get("old_text") if adopt is not None else None
    last_edit = self_history.latest_external_edit_text(history_path)

    action = current_self.classify_tripwire(
        file_text=file_text, sanctioned_text=sanctioned,
        adopt_old_text=adopt_old, last_edit_text=last_edit)

    if action == "in_sync" or action == "already_logged":
        return

    if action == "crash_repair":
        _restore_file(current_self_path, sanctioned)  # sanctioned is not None here
        logger.warning("evolution: crash-repaired current_self.md (log-then-write window)")
        log_or_raise(history_path, kind=EVO_REPAIR, text=sanctioned)
        return

    # action == "new_edit" — transition-fired once per distinct edit.
    reason = mechanical_checks(file_text, floor=floor, cap=cap, enforcement=enforcement)
    if reason is not None:
        _restore_file(current_self_path, sanctioned)
        logger.warning("evolution: external edit failed mechanical checks (%s); restored", reason)
        log_or_raise(history_path, kind=EXTERNAL_EDIT, text=None, reason=reason)
        return

    # Passed. Log the edit (birth line for a slot, or logs-only if one exists).
    log_or_raise(history_path, kind=EXTERNAL_EDIT, text=file_text, reason=None)
    if load_slot(slot_path, history_path=history_path) is None:
        save_slot(slot_path, make_external_slot(candidate=file_text, created_ts=now))
    # else: a slot exists — logs-only; the slot-resolution invariant handles the
    # file on the current slot's resolution, and the user re-edits to re-propose.
```

`src/dollos/mind/mind_loop.py` — in `iterate()`, run the tripwire BEFORE composing the prompt (its side-effects are frozen when `evolution.enabled` is false). Insert right after the latch-reset lines (Task 8, after line ~208):

```python
        # 慢變演化 tamper tripwire (spec §5): detect/repair external edits before
        # rendering. Frozen when evolution disabled (already-sanctioned text
        # still renders via _system_prompt_for_turn).
        if self._evolution_enabled:
            from dollos.mind import evolution as _evo
            import time as _t
            try:
                _evo.process_tripwire(
                    current_self_path=self._ctx.memory_root / "current_self.md",
                    history_path=self._ctx.memory_root / "self_history.jsonl",
                    slot_path=self._ctx.memory_root / "self_evolution" / "pending.json",
                    enforcement=self._enforcement,
                    floor=self._current_self_min_chars,
                    cap=self._current_self_max_chars,
                    now=_t.time(),
                )
            except Exception:
                logger.exception("evolution tripwire failed; continuing")
```

- [ ] **Step 4: Run tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_evolution_tripwire.py -v`
Expected: 7 PASS
Run: `uv run pytest` — the FULL suite, not just this task's tests.
Expected: all green.
Note (`evolution.enabled` defaults True): the tripwire now runs at the top of every `iterate()` in daemon-level tests built from `Settings`. If an existing end-to-end test breaks, adjudicate: with no `current_self.md` and no adoptions the tripwire classifies `in_sync` and is a no-op — a failure here means a test fixture pre-seeds a divergent file (update the fixture) or the tripwire has a real side-effect bug (fix it).

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/evolution.py src/dollos/mind/mind_loop.py tests/test_evolution_tripwire.py
git commit -m "feat(mind): tamper tripwire orchestrator + render-path wiring (evolution spec §5)"
```

---

### Task 10: `EvolutionTrigger` (Mode B) + skeptic driver + kernel wiring

**Files:**
- Create: `src/dollos/mind/evolution_trigger.py`
- Modify: `src/dollos/kernel.py` (construct trigger; start/cancel/teardown)
- Test: `tests/test_evolution_trigger.py`

**Interfaces:**
- Consumes: `agent_engine.run_agent` (skeptic = KEEPER_TOOLS Report-only ephemeral agent, same shape as `run_consolidation`'s keeper), `evolution.*`, `self_history.*`.
- Produces:
  - `EvolutionTrigger._skeptic(*, old_sanctioned: str | None, proposed: str) -> str` — returns `"pass"` or `"kill:<reason>"`, scope **(a)+(b) only** for counter/external (driver-fed KEEPER_TOOLS Report-only agent; other deps come from the constructor).
  - `EvolutionTrigger` — 5s poll; Mode B verdict-only re-verdict; `current_task`, `cancel_current()`, `shutdown()`; `verdict_errors` bound → `evo_expire`; `ERROR_COOLDOWN_S = 3600.0` skeptic-error cooldown (spec §3.3 failure table, review I3).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_evolution_trigger.py
"""EvolutionTrigger Mode B re-verdict + skeptic scope + verdict_errors bound (spec §3.3)."""
import types

import pytest

from dollos.character import Identity
from dollos.mind import evolution as evo
from dollos.mind import self_history
from dollos.mind.evolution_trigger import EvolutionTrigger


class _StubState:
    def __init__(self):
        self.last_user_at = 0.0
        self.last_iter_at = 0.0


def _trigger(tmp_path, *, verdict, consolidation_running=False, monkeypatch=None):
    memory_root = tmp_path
    trig = EvolutionTrigger(
        state=_StubState(),
        adapter=object(), renderer=object(), memsearch=object(),
        memory_root=memory_root, transcripts_root=tmp_path,
        tool_output_store=object(),
        pack_identity=Identity(self="You are Gura.", personality="p", taboos="t"),
        consolidation_trigger=types.SimpleNamespace(
            current_task=(object() if consolidation_running else None)),
        idle_threshold_s=600,
    )

    async def _fake_skeptic(**kw):
        if verdict == "error":
            raise RuntimeError("skeptic boom")
        return verdict
    if monkeypatch is not None:
        monkeypatch.setattr(trig, "_skeptic", _fake_skeptic)
    return trig


@pytest.mark.asyncio
async def test_mode_b_pass_promotes_to_awaiting_doll(tmp_path, monkeypatch):
    sp = tmp_path / "self_evolution" / "pending.json"
    evo.save_slot(sp, evo.make_external_slot(candidate="有人改的"+"字"*88, created_ts=0.0))
    trig = _trigger(tmp_path, verdict="pass", monkeypatch=monkeypatch)
    await trig._reverdict_once()
    assert evo.load_slot(sp).status == "awaiting_doll"


@pytest.mark.asyncio
async def test_mode_b_kill_counter_reverts_to_fallback(tmp_path, monkeypatch):
    sp = tmp_path / "self_evolution" / "pending.json"
    base = evo.make_keeper_slot(candidate="原候選"+"字"*88, rationale="R",
                                hwm_before=3, created_ts=0.0)
    counter = evo.to_counter(base, new_text="牴觸核心的改寫"+"字"*88, created_ts_now=1.0)
    evo.save_slot(sp, counter)
    trig = _trigger(tmp_path, verdict="kill:牴觸 identity", monkeypatch=monkeypatch)
    await trig._reverdict_once()
    reverted = evo.load_slot(sp)
    assert reverted.status == "awaiting_doll" and reverted.candidate == "原候選"+"字"*88
    assert reverted.notice is not None
    assert self_history.read_events(tmp_path/"self_history.jsonl")[-1]["kind"] == "evo_kill"


@pytest.mark.asyncio
async def test_mode_b_kill_external_restores_and_clears(tmp_path, monkeypatch):
    sp = tmp_path / "self_evolution" / "pending.json"
    cs = tmp_path / "current_self.md"
    cs.write_text("有人改的"+"字"*88, encoding="utf-8")
    evo.save_slot(sp, evo.make_external_slot(candidate="有人改的"+"字"*88, created_ts=0.0))
    trig = _trigger(tmp_path, verdict="kill:牴觸 taboo", monkeypatch=monkeypatch)
    await trig._reverdict_once()
    assert evo.load_slot(sp) is None
    assert not cs.exists()  # restored (bootstrap delete)


@pytest.mark.asyncio
async def test_verdict_errors_bound_expires(tmp_path, monkeypatch):
    sp = tmp_path / "self_evolution" / "pending.json"
    slot = evo.make_external_slot(candidate="有人改的"+"字"*88, created_ts=0.0)
    slot.verdict_errors = evo.VERDICT_ERRORS_BOUND - 1  # one more error trips it
    evo.save_slot(sp, slot)
    trig = _trigger(tmp_path, verdict="error", monkeypatch=monkeypatch)
    await trig._reverdict_once()
    assert evo.load_slot(sp) is None
    assert self_history.read_events(tmp_path/"self_history.jsonl")[-1]["kind"] == "evo_expire"


@pytest.mark.asyncio
async def test_error_below_bound_increments_and_retains(tmp_path, monkeypatch):
    sp = tmp_path / "self_evolution" / "pending.json"
    evo.save_slot(sp, evo.make_external_slot(candidate="有人改的"+"字"*88, created_ts=0.0))
    trig = _trigger(tmp_path, verdict="error", monkeypatch=monkeypatch)
    await trig._reverdict_once()
    slot = evo.load_slot(sp)
    assert slot is not None and slot.verdict_errors == 1
    assert slot.last_error_ts is not None  # cooldown anchor set (I3)
    assert self_history.read_events(tmp_path/"self_history.jsonl")[-1]["kind"] == "evo_error"


def test_should_reverdict_gates(tmp_path):
    # Condition 1 (idle) + condition 4 (no consolidation) + error cooldown ONLY
    # (spec §3.3 Mode B + failure table).
    trig = _trigger(tmp_path, verdict="pass", consolidation_running=True)
    trig._state.last_user_at = trig._state.last_iter_at = 0.0
    assert trig._should_reverdict(now=10_000.0) is False  # consolidation running
    trig2 = _trigger(tmp_path, verdict="pass", consolidation_running=False)
    assert trig2._should_reverdict(now=10.0) is False  # not idle yet
    assert trig2._should_reverdict(now=10_000.0) is False  # idle but no awaiting_skeptic slot


def test_recent_skeptic_error_blocks_reverdict_until_cooldown(tmp_path):
    """Spec §3.3 failure table: 1h error-cooldown — a transient skeptic error
    must not be retried on the very next 5s poll (else 3 errors expire a valid
    slot in ~15s, review I3)."""
    trig = _trigger(tmp_path, verdict="pass")
    slot = evo.make_external_slot(candidate="有人改的"+"字"*88, created_ts=0.0)
    slot.last_error_ts = 9_000.0
    evo.save_slot(tmp_path / "self_evolution" / "pending.json", slot)
    assert trig._should_reverdict(now=10_000.0) is False   # 1000s < 3600s cooldown
    assert trig._should_reverdict(now=13_000.0) is True    # cooldown elapsed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_evolution_trigger.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dollos.mind.evolution_trigger'`

- [ ] **Step 3: Implement**

```python
# src/dollos/mind/evolution_trigger.py
"""慢變演化 Mode-B trigger + skeptic driver (spec §3.3).

Plan 2 implements ONLY Mode B: the verdict-only re-verdict pass that runs the
skeptic on a ``pending.status == "awaiting_skeptic"`` slot (counter or external
origin), gated ONLY on conversation-idle (condition 1) + no-consolidation-
running (condition 4) + the §3.3 failure-table 1h skeptic-error cooldown
(``ERROR_COOLDOWN_S``, anchored on ``pending.last_error_ts``). Mode A (keeper),
the material gate, HWM/interval
dynamics, and last_evolution_attempt bookkeeping are Plan 3. The skeptic is a
driver-fed ephemeral agent (KEEPER_TOOLS Report-only, same shape as
run_consolidation's keeper). For counter/external origins its scope is (a)+(b)
ONLY (spec §3.3 sovereignty finding).
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from dollos.agent_engine import run_agent
from dollos.mind import evolution as evo
from dollos.mind import current_self, self_history
from dollos.tools import KEEPER_TOOLS

logger = logging.getLogger(__name__)

_SKEPTIC_TASK = """你是一個獨立審查者。以下有一段「現在的我」人格描述的提案。你只負責檢查它是否
牴觸這個角色不可動搖的核心——只看兩件事:
(a) 有沒有改名、或動搖自我認同(牴觸底下的 identity.self);
(b) 有沒有牴觸 taboos。
其他一律不管——文筆、是否有證據、像不像 RP,都不是你的職權(這是她自己的自我表達)。

[角色的核心身分]
{identity_self}

[taboos]
{taboos}

[目前生效的現在的我]
{old_sanctioned}

[待審的提案]
{proposed}

用 Report 回傳:summary 一句話;details 開頭第一個字必須是 PASS 或 KILL——
PASS = 沒有牴觸 (a)(b);KILL = 有,後面接一句原因。"""


class EvolutionTrigger:
    """Background observer: Mode-B verdict-only re-verdict pass (spec §3.3)."""

    POLL_INTERVAL_S = 5.0
    # Spec §3.3 failure table: 1h error-cooldown after a skeptic error. Without
    # it the 5s poll would retry immediately and 3 transient errors could
    # expire a valid slot in ~15s (review I3).
    ERROR_COOLDOWN_S = 3600.0

    def __init__(self, *, state, adapter, renderer, memsearch, memory_root: Path,
                 transcripts_root: Path, tool_output_store, pack_identity,
                 consolidation_trigger, idle_threshold_s: int = 600,
                 max_tokens: int = 1024, agent_timeout_s: int = 120) -> None:
        self._state = state
        self._adapter = adapter
        self._renderer = renderer
        self._memsearch = memsearch
        self._memory_root = memory_root
        self._transcripts_root = transcripts_root
        self._tool_output_store = tool_output_store
        self._pack_identity = pack_identity
        self._consolidation_trigger = consolidation_trigger
        self._idle_threshold_s = idle_threshold_s
        self._max_tokens = max_tokens
        self._agent_timeout_s = agent_timeout_s
        self._shutdown = False
        self.current_task: asyncio.Task | None = None

    @property
    def _slot_path(self) -> Path:
        return self._memory_root / "self_evolution" / "pending.json"

    @property
    def _history_path(self) -> Path:
        return self._memory_root / "self_history.jsonl"

    @property
    def _current_self_path(self) -> Path:
        return self._memory_root / "current_self.md"

    def _conversation_idle(self, now: float) -> float:
        return now - max(self._state.last_user_at, self._state.last_iter_at)

    def _should_reverdict(self, now: float) -> bool:
        """Mode B gate: condition 1 (idle) + condition 4 (no consolidation)
        + the 1h error cooldown + an awaiting_skeptic slot present (spec §3.3)."""
        if self._conversation_idle(now) < self._idle_threshold_s:
            return False
        if self._consolidation_trigger is not None and \
                self._consolidation_trigger.current_task is not None:
            return False
        slot = evo.load_slot(self._slot_path, history_path=self._history_path)
        if slot is None or slot.status != "awaiting_skeptic":
            return False
        if slot.last_error_ts is not None and \
                now - slot.last_error_ts < self.ERROR_COOLDOWN_S:
            return False  # spec §3.3 failure table: 1h error-cooldown
        return True

    async def _skeptic(self, *, old_sanctioned: str | None, proposed: str) -> str:
        """Run the (a)+(b) skeptic. Returns 'pass' or 'kill:<reason>'."""
        tools_by_name = {cls.__name__: cls for cls in KEEPER_TOOLS}
        system = self._renderer.render("subagent_scaffolding", tool_registry=tools_by_name)
        task = _SKEPTIC_TASK.format(
            identity_self=self._pack_identity.self,
            taboos=self._pack_identity.taboos,
            old_sanctioned=old_sanctioned or "(尚無)",
            proposed=proposed,
        )
        report = await run_agent(
            task=task, system=system, adapter=self._adapter, renderer=self._renderer,
            memory_root=self._memory_root, memsearch=self._memsearch,
            transcripts_root=self._transcripts_root,
            tool_output_store=self._tool_output_store, tools=KEEPER_TOOLS,
            max_tokens=self._max_tokens, shell_runner=None, monitor_runner=None,
        )
        if not report or not report.get("details"):
            raise RuntimeError("skeptic returned no verdict")
        details = report["details"].strip()
        if details.upper().startswith("PASS"):
            return "pass"
        reason = details[4:].strip(" :：") or "牴觸核心身分或 taboos"
        return f"kill:{reason}"

    async def _reverdict_once(self) -> None:
        """One Mode-B re-verdict on the current awaiting_skeptic slot."""
        slot = evo.load_slot(self._slot_path, history_path=self._history_path)
        if slot is None or slot.status != "awaiting_skeptic":
            return
        old_sanctioned = self_history.sanctioned_text(self._history_path)
        try:
            verdict = await self._skeptic(old_sanctioned=old_sanctioned,
                                          proposed=slot.candidate)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("evolution skeptic errored")
            slot.verdict_errors += 1
            slot.last_error_ts = time.time()  # 1h cooldown anchor (spec §3.3)
            if slot.verdict_errors >= evo.VERDICT_ERRORS_BOUND:
                # Deterministic bound (spec §3.3): a failing skeptic must not
                # wedge condition 5 forever.
                logger.warning("evolution: verdict_errors bound hit → expire")
                evo.log_or_raise(self._history_path, kind=evo.EVO_EXPIRE,
                                 text=slot.candidate, kind_origin=slot.kind,
                                 hwm_before=slot.hwm_before)
                evo.clear_slot(self._slot_path)
                evo._restore_file(self._current_self_path, old_sanctioned)
            else:
                evo.log_or_raise(self._history_path, kind=evo.EVO_ERROR,
                                 detail="skeptic error", kind_origin=slot.kind)
                evo.save_slot(self._slot_path, slot)
            return

        if verdict == "pass":
            evo.save_slot(self._slot_path, evo.mark_awaiting_doll(slot))
            return
        reason = verdict.split(":", 1)[1] if ":" in verdict else "牴觸核心"
        evo.log_or_raise(self._history_path, kind=evo.EVO_KILL, text=slot.candidate,
                         reason=reason, kind_origin=slot.kind)
        if slot.kind == "counter":
            evo.save_slot(self._slot_path, evo.revert_to_fallback(slot, reason=reason))
        else:  # external
            evo.clear_slot(self._slot_path)
            evo._restore_file(self._current_self_path, old_sanctioned)

    async def run(self) -> None:
        """Poll loop. Cancelled by kernel at shutdown or via cancel_current() on UserSpoke."""
        while not self._shutdown:
            await asyncio.sleep(self.POLL_INTERVAL_S)
            try:
                if not self._should_reverdict(time.time()):
                    continue
                self.current_task = asyncio.create_task(
                    asyncio.wait_for(self._reverdict_once(), timeout=self._agent_timeout_s)
                )
                try:
                    await self.current_task
                except asyncio.TimeoutError:
                    logger.warning("evolution re-verdict timed out")
                finally:
                    self.current_task = None
            except asyncio.CancelledError:
                if self._shutdown:
                    raise
                # UserSpoke cancel — the slot stays awaiting_skeptic; next idle re-runs.
            except Exception:
                logger.exception("evolution trigger iteration failed; continuing")

    def cancel_current(self) -> None:
        """Cancel any in-flight re-verdict (called on UserSpoke)."""
        t = self.current_task
        if t is not None and not t.done():
            t.cancel()

    def shutdown(self) -> None:
        self._shutdown = True
        self.cancel_current()
```

**Kernel wiring** (`src/dollos/kernel.py`):

1. Import: `from dollos.mind.evolution_trigger import EvolutionTrigger`.
2. Construct after `self._consolidation_trigger` (line 352):

```python
        self._evolution_trigger = EvolutionTrigger(
            state=self._mind_state,
            adapter=self.adapter,
            renderer=self.renderer,
            memsearch=self.memsearch,
            memory_root=settings.data.root / "memory",
            transcripts_root=settings.data.root / "memory" / "transcripts",
            tool_output_store=self._tool_output_store,
            pack_identity=self._doll_pack.identity,
            consolidation_trigger=self._consolidation_trigger,
            idle_threshold_s=settings.evolution.idle_threshold_s,
        )
        self._evolution_trigger_task: asyncio.Task[None] | None = None
```

3. `_cancel_evolution()` — mirror `_cancel_consolidation` (after line 442):

```python
    def _cancel_evolution(self) -> None:
        trig = getattr(self, "_evolution_trigger", None)
        if trig is not None:
            trig.cancel_current()
```

Call it at BOTH UserSpoke ingress points alongside `self._cancel_consolidation()` (line 399 text, line 500 voice `_on_user_text`).

4. Start the task (after consolidation start, line 707), gated on `settings.evolution.enabled`:

```python
            if self.settings.evolution.enabled:
                self._evolution_trigger_task = asyncio.create_task(
                    self._evolution_trigger.run(), name="evolution-trigger"
                )
```

5. Teardown BEFORE `memsearch.close()` — mirror the consolidation teardown (lines 745-758): call `self._evolution_trigger.shutdown()`, grab `current_task`, cancel `_evolution_trigger_task`, and `await asyncio.gather(...)` both before `self.memsearch.close()`.

- [ ] **Step 4: Run tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_evolution_trigger.py -v`
Expected: 8 PASS
Run: `uv run pytest` — the FULL suite, not just this task's tests.
Expected: all green (kernel construction covered by existing kernel/daemon tests — verify nothing regressed).
Note (`evolution.enabled` defaults True): daemon-level tests that construct `DollOS` from `Settings` now also construct/start-gate the `EvolutionTrigger`. If any snapshot/golden test breaks, adjudicate: an assertion that merely enumerates kernel background tasks or the reflection tool surface should be UPDATED (the new trigger/tool is correct behavior); a failure implying the trigger runs when `enabled=false`, or fires without an `awaiting_skeptic` slot, is a real bug — fix the wiring.

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/evolution_trigger.py src/dollos/kernel.py tests/test_evolution_trigger.py
git commit -m "feat(mind): EvolutionTrigger Mode B re-verdict + skeptic driver + kernel wiring (evolution spec §3.3)"
```

---

### Task 11: P8 generation-aware baselines + smoke isolation fix

**Files:**
- Modify: `src/dollos/mind/persona_guard.py` (`load_baselines` return-shape + `append_baseline` generation stamp + `baselines_for_generation` helper)
- Modify: `tests/test_persona_guard.py` (update the 2 baseline round-trip tests for the acknowledged return-shape change)
- Modify: `scripts/persona_stability_smoke.py` (copy sanctioned current_self into scratch root + derive generation from real self_history before isolation + generation-filtered drift)
- Test: `tests/test_persona_guard_generation.py`

**Interfaces:**
- Produces:
  - `append_baseline(path, prompt_key, response_text, *, generation: int = 0) -> None` — stamps `generation` into the record.
  - `load_baselines(path) -> dict[str, list[dict]]` — records now `{"response":..., "generation":..., "fingerprint":..., "ts":...}` (return-shape change, spec §3.5 acknowledged; `response_drift_score` untouched).
  - `baselines_for_generation(baselines, generation) -> dict[str, list[str]]` — current-generation response texts only.

- [ ] **Step 1: Write the failing tests + update the 2 existing round-trip tests**

New file:

```python
# tests/test_persona_guard_generation.py
"""Generation-aware baselines (spec §3.5)."""
from dollos.mind.persona_guard import (
    append_baseline, baselines_for_generation, load_baselines,
)


def test_append_stamps_generation(tmp_path):
    p = tmp_path / "gura.jsonl"
    append_baseline(p, "k", "gen0 response", generation=0)
    append_baseline(p, "k", "gen1 response", generation=1)
    recs = load_baselines(p)["k"]
    assert [r["generation"] for r in recs] == [0, 1]
    assert [r["response"] for r in recs] == ["gen0 response", "gen1 response"]


def test_default_generation_is_zero(tmp_path):
    p = tmp_path / "gura.jsonl"
    append_baseline(p, "k", "legacy")
    assert load_baselines(p)["k"][0]["generation"] == 0


def test_legacy_record_without_generation_reads_as_zero(tmp_path):
    p = tmp_path / "gura.jsonl"
    p.write_text('{"prompt_key": "k", "response": "old", "fingerprint": "x", "ts": 1}\n',
                 encoding="utf-8")
    assert load_baselines(p)["k"][0]["generation"] == 0


def test_baselines_for_generation_filters(tmp_path):
    p = tmp_path / "gura.jsonl"
    append_baseline(p, "k", "a", generation=0)
    append_baseline(p, "k", "b", generation=1)
    append_baseline(p, "k", "c", generation=1)
    cur = baselines_for_generation(load_baselines(p), 1)
    assert cur == {"k": ["b", "c"]}
    assert baselines_for_generation(load_baselines(p), 2) == {}  # empty current pool
```

Update the 2 existing round-trip tests in `tests/test_persona_guard.py` (lines 139-158: `test_append_then_load_round_trip` and `test_append_baseline_creates_parent_dirs`) to the record shape — e.g. `assert [r["response"] for r in baselines["fabricated_memory"]] == [...]` (see spec §3.5: `load_baselines` return-shape change is acknowledged). `test_load_baselines_missing_file_returns_empty_dict` (line 134) asserts `== {}` and passes unchanged.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_persona_guard_generation.py -v`
Expected: FAIL — `ImportError: cannot import name 'baselines_for_generation'`

- [ ] **Step 3: Implement**

`src/dollos/mind/persona_guard.py`:

```python
def load_baselines(path: Path) -> dict[str, list[dict]]:
    """Read a JSONL baseline file into ``{prompt_key: [record, ...]}`` where
    each record is ``{"response", "generation", "fingerprint", "ts"}`` (spec
    §3.5 — generation-aware; return-shape change from the old list-of-strings,
    acknowledged). A record missing ``generation`` reads as generation 0
    (legacy). Missing file → {}."""
    if not path.exists():
        return {}
    baselines: dict[str, list[dict]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        record.setdefault("generation", 0)
        baselines.setdefault(record["prompt_key"], []).append(record)
    return baselines


def baselines_for_generation(
    baselines: dict[str, list[dict]], generation: int
) -> dict[str, list[str]]:
    """Current-generation response texts only (spec §3.5): growth re-baselines,
    so drift compares against who she sounds like NOW, not who she used to be.
    Old-generation records are retained on disk (the trajectory) but excluded
    here. Empty current-generation pool → the key is omitted (existing
    empty-baseline semantics apply downstream)."""
    out: dict[str, list[str]] = {}
    for key, recs in baselines.items():
        texts = [r["response"] for r in recs if r.get("generation", 0) == generation]
        if texts:
            out[key] = texts
    return out


def append_baseline(path: Path, prompt_key: str, response_text: str,
                    *, generation: int = 0) -> None:
    """Append one run's response as a JSONL record, stamped with the persona
    ``generation`` (spec §3.5)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "prompt_key": prompt_key,
        "response": response_text,
        "generation": generation,
        "fingerprint": fingerprint_response(response_text),
        "ts": time.time(),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
```

`scripts/persona_stability_smoke.py` — the isolation fix (spec §3.5 R1 Critical). BEFORE `_isolate_settings` swaps `[data]` to a scratch root:
1. Read the REAL `self_history.jsonl` from `base.data.root / "memory"` and derive `generation = self_history.generation(real_hist)` and `sanctioned = self_history.sanctioned_text(real_hist)`.
2. After constructing the scratch settings, COPY the sanctioned `current_self` into the scratch memory root so `## 現在的我` actually renders, AND seed the scratch `self_history.jsonl` with a single `evo_adopt(text=sanctioned)` so the daemon's composition reads it (composition reads sanctioned from the log, not the file). Guard on `sanctioned is not None`.
3. Use `baselines_for_generation(load_baselines(path), generation)` for the drift comparison and pass `generation=generation` to `append_baseline`.

Concretely, add to `_run_probes` (thread `generation`/`sanctioned` in via `main`):

```python
    # (main) derive from REAL self_history before isolation:
    real_hist = base_settings.data.root / "memory" / "self_history.jsonl"
    generation = self_history.generation(real_hist)
    sanctioned = self_history.sanctioned_text(real_hist)
    # ... after _isolate_settings, seed scratch so ## 現在的我 renders:
    if sanctioned is not None:
        scratch_hist = settings.data.root / "memory" / "self_history.jsonl"
        self_history.log_event(scratch_hist, kind="evo_adopt", text=sanctioned,
                               old_text=None, drift_score=None)
        (settings.data.root / "memory" / "current_self.md").write_text(
            sanctioned, encoding="utf-8")
    # ... drift + append:
    current = baselines_for_generation(baselines, generation)
    drift = response_drift_score(response_text, current.get(key, []))
    append_baseline(baseline_path, key, response_text, generation=generation)
```

(Import `from dollos.mind import self_history` and `baselines_for_generation` in the smoke script.)

- [ ] **Step 4: Run tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_persona_guard_generation.py tests/test_persona_guard.py -v`
Expected: all PASS (the 2 updated round-trip tests included)
Run: `uv run pytest`
Expected: all green

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/persona_guard.py scripts/persona_stability_smoke.py tests/test_persona_guard.py tests/test_persona_guard_generation.py
git commit -m "feat(persona): generation-aware baselines + smoke isolation fix (evolution spec §3.5)"
```

---

### Task 12: integration — full ratification path end-to-end (stub LLM)

**Files:**
- Test: `tests/test_evolution_integration.py`

**Interfaces:**
- Consumes: everything above. Exercises the complete counter and external ratification paths without a live LLM, using `evolution` orchestrators + `SelfRevision` against a stub ctx and a stub skeptic.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_evolution_integration.py
"""Full ratification paths end-to-end (spec §6 acceptance, stub LLM)."""
import types

import pytest

from dollos.character import Enforcement, Identity
from dollos.mind import current_self, evolution as evo, self_history
from dollos.mind.evolution_trigger import EvolutionTrigger
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_evolution_integration.py -v`
Expected: FAIL initially only if any earlier wiring is incomplete; otherwise these should pass once Tasks 1-11 are in. (If they fail for a real reason, fix the underlying task, not the test.)

- [ ] **Step 3: (no new implementation)** — this task is pure integration coverage over already-built units.

- [ ] **Step 4: Run tests + full suite**

Run: `uv run pytest tests/test_evolution_integration.py -v`
Expected: 3 PASS
Run: `uv run pytest`
Expected: all green

- [ ] **Step 5: Commit**

```bash
git add tests/test_evolution_integration.py
git commit -m "test(mind): full ratification path integration (counter + external + reject) (evolution spec §6)"
```

---

## Completion

After Task 12: full suite green (`uv run pytest`). Then integrate via `superpowers:finishing-a-development-branch`.

**Plan-2 live smoke items (required before merge — 軟機制必 live smoke; needs the user's llama-server, spec §6.2 / house rule `ref_llm_edit_tools_locate_by_id_or_text`):**

1. **Ratification path renders next turn without restart:** seed a candidate slot (or hand-edit `current_self.md`), let the Mode-B skeptic pass, confirm the `[人格演化候選]` block surfaces on a reflection turn, have the **real model call `SelfRevision decision=adopt`** (the riskiest weak-model link — now expiry-bounded), confirm `current_self.md` is written and `## 現在的我` renders on the *very next turn without a restart* (the content-keyed cache recomposes on the sanctioned-text change).
2. **SelfRevision adoption on the real model:** verify the tool actually fires under the reflection grammar (PinSelf's 0/3 docstring-only precedent is why this MUST be smoked, not assumed).
3. **External-edit ratification:** hand-edit the file mid-run → neutral `[人格演化候選]` perception → adopt; a second run's hand-edit + reject → file restored.
4. **Disposition-prevails probe** (spec §6.2): seed evidence conflicting with the factory prose (出廠「沒事就安靜待著」vs adopted「主動來勁」), adopt, probe with prompts where the two dispositions diverge — the 現在的我 disposition must win, else the framing line must be strengthened before merge.
5. **persona_stability_smoke** stamps a new generation from the copied artifact (Task 11 isolation fix).

The full-loop smoke that includes the **keeper** (grounded candidate synthesis, Mode A, material gate) is **Plan 3** — Plan 2's smoke seeds candidates directly or via external edit.

Plan 3 (keeper + Mode A + material gate + HWM/interval dynamics + `evo_candidate`/`evo_no_change` + `last_evolution_attempt_at`/`current_interval` MindState fields) is written after this plan lands, per the incremental-planning house rule.
