# Self-Evolution Evidence Layer (Plan 1 of 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `self_history.jsonl` — the append-only evidence log the 慢變演化 keeper will read: every PinSelf mutation recorded with tombstones (removed/replaced text preserved), cross-turn-only reconfirm signals, and an `external_ctx` provenance flag.

**Architecture:** A new pure module `self_history.py` (append/scan helpers, no LLM, mirror of `self_profile.py`'s standalone style); `self_profile.apply()` gains optional logging params and logs after each successful mutation (pin-IO-swallow rule); `MindCtx` gains two per-turn fields (`current_turn`, `external_ctx`) that `MindLoop.iterate` sets at drain time and the Recall refeed path upgrades; `PinSelf.run` threads them through. Spec: `docs/superpowers/specs/2026-07-02-slow-self-evolution-design.md` §3.2 (this plan implements §3.2's pin events ONLY; `evo_*` events belong to Plan 3).

**Tech Stack:** Python 3.12, pydantic tools, pytest, `uv run pytest`.

## Global Constraints

- `self_history.jsonl` lives at `{memory_root}/self_history.jsonl` — memory_root ROOT, never under `shared/`, never FTS-indexed, never injected into any prompt block (spec §3.2).
- Pin-event log failures are swallowed loudly (`logger.exception`), never break the pin itself — this swallow rule is pins-only (spec §3.2).
- `pin_reconfirm` logs ONLY cross-turn: a dedup hit in the same outer turn as the last `pin_add`/`pin_reconfirm` of that text is a refeed artifact and must NOT log (spec §3.2).
- `turn` = the mind-loop OUTER iteration number (`MindState.iter_count` at drain); all cascade/refeed passes of one turn share one value.
- `external_ctx` := drained batch contains `ToolResultArrived`/`MonitorFired`/`MonitorEnded` OR Recall executed earlier in this turn's cascade. The `[Memory context]` auto-block does NOT count (spec §3.2, conscious choice).
- Cap-rejected writes log nothing (the mutation was discarded).
- No fallback mechanisms; friendly-error norms unchanged; all 873 existing tests stay green.
- Worktree: `.worktrees/self-evolution-evidence-layer/`, branch `self-evolution-evidence-layer` (per-plan worktree rule).

---

### Task 1: `self_history` module (append + backward scan)

**Files:**
- Create: `src/dollos/mind/self_history.py`
- Test: `tests/test_self_history.py`

**Interfaces:**
- Produces: `log_event(path: Path, *, kind: str, **fields) -> None` (adds `ts`, appends one JSON line, RAISES `OSError` on IO failure — caller decides swallow); `last_pin_turn(path: Path, *, section: str, text: str) -> int | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_self_history.py
"""self_history — append-only evidence log (spec 2026-07-02 slow-self-evolution §3.2)."""
import json

from dollos.mind import self_history


def test_log_event_appends_jsonl_with_ts(tmp_path):
    p = tmp_path / "self_history.jsonl"
    self_history.log_event(p, kind="pin_add", turn=3, external_ctx=False,
                           section="self", id="s1", text="喜歡看監控數字跳動")
    self_history.log_event(p, kind="pin_remove", turn=5, external_ctx=False,
                           section="self", id="s1", text="", old_text="喜歡看監控數字跳動")
    lines = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    assert [e["kind"] for e in lines] == ["pin_add", "pin_remove"]
    assert lines[0]["turn"] == 3 and "ts" in lines[0]
    assert lines[1]["old_text"] == "喜歡看監控數字跳動"  # tombstone preserved


def test_log_event_creates_parent_dir(tmp_path):
    p = tmp_path / "deep" / "self_history.jsonl"
    self_history.log_event(p, kind="pin_add", turn=1, external_ctx=True,
                           section="user", id="u1", text="主人熬夜")
    assert json.loads(p.read_text().splitlines()[0])["external_ctx"] is True


def test_last_pin_turn_finds_most_recent_matching(tmp_path):
    p = tmp_path / "self_history.jsonl"
    self_history.log_event(p, kind="pin_add", turn=3, external_ctx=False,
                           section="self", id="s1", text="A")
    self_history.log_event(p, kind="pin_reconfirm", turn=9, external_ctx=False,
                           section="self", id="s1", text="A")
    self_history.log_event(p, kind="pin_add", turn=11, external_ctx=False,
                           section="user", id="u1", text="A")  # other section, same text
    assert self_history.last_pin_turn(p, section="self", text="A") == 9
    assert self_history.last_pin_turn(p, section="user", text="A") == 11
    assert self_history.last_pin_turn(p, section="self", text="B") is None


def test_last_pin_turn_ignores_non_pin_kinds(tmp_path):
    p = tmp_path / "self_history.jsonl"
    self_history.log_event(p, kind="pin_remove", turn=4, external_ctx=False,
                           section="self", id="s1", text="", old_text="A")
    assert self_history.last_pin_turn(p, section="self", text="A") is None


def test_last_pin_turn_missing_file(tmp_path):
    assert self_history.last_pin_turn(tmp_path / "nope.jsonl", section="self", text="A") is None


def test_last_pin_turn_tolerates_torn_tail_line(tmp_path):
    p = tmp_path / "self_history.jsonl"
    self_history.log_event(p, kind="pin_add", turn=2, external_ctx=False,
                           section="self", id="s1", text="A")
    with p.open("a", encoding="utf-8") as f:
        f.write('{"kind": "pin_add", "turn"')  # torn write, no newline-terminated JSON
    assert self_history.last_pin_turn(p, section="self", text="A") == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_self_history.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dollos.mind.self_history'`

- [ ] **Step 3: Write the implementation**

```python
# src/dollos/mind/self_history.py
"""慢變演化 evidence layer — append-only event log (spec 2026-07-02 §3.2).

Records every self_profile mutation (tombstones included) and, in Plan 3,
the evolution-machinery events. Append-only; never FTS-indexed; never
injected into any prompt block. Pure module, no LLM — mirror of
self_profile.py's standalone, unit-testable style.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def log_event(path: Path, *, kind: str, **fields) -> None:
    """Append one event line (adds ``ts``). RAISES OSError on IO failure —
    the caller decides the swallow policy (pin events: swallow loudly;
    evolution events: abort — spec §3.2 write-ordering rules)."""
    event = {"ts": time.time(), "kind": kind, **fields}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
        f.flush()


def last_pin_turn(path: Path, *, section: str, text: str) -> int | None:
    """Outer-turn number of the most recent ``pin_add``/``pin_reconfirm`` for
    this exact section+text; None if never logged. Backward scan of the small
    jsonl file — deliberately not an in-memory map, so the cross-turn
    reconfirm rule survives restarts (spec §3.2)."""
    if not path.exists():
        return None
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue  # torn tail line — tolerate, keep scanning
        if (ev.get("kind") in ("pin_add", "pin_reconfirm")
                and ev.get("section") == section and ev.get("text") == text):
            turn = ev.get("turn")
            return int(turn) if isinstance(turn, int) else None
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_self_history.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/self_history.py tests/test_self_history.py
git commit -m "feat(mind): self_history append-only evidence log (evolution spec §3.2)"
```

---

### Task 2: `self_profile.apply` logs pin events (tombstones + cross-turn reconfirm)

**Files:**
- Modify: `src/dollos/mind/self_profile.py` (module imports + `apply()` — current signature at line 132: `apply(path, *, section, op, target, text, max_chars, today) -> str`)
- Test: `tests/test_self_profile_history.py`

**Interfaces:**
- Consumes: `self_history.log_event`, `self_history.last_pin_turn` (Task 1).
- Produces: `apply(path, *, section, op, target, text, max_chars, today, history_path: Path | None = None, turn: int = 0, external_ctx: bool = False) -> str` — same return values as today; `history_path=None` disables logging entirely (back-compat: every existing caller/test is untouched).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_self_profile_history.py
"""self_profile.apply → self_history pin-event logging (spec 2026-07-02 §3.2)."""
import json

import pytest

from dollos.mind import self_profile, self_history


def _events(hist):
    if not hist.exists():
        return []
    return [json.loads(l) for l in hist.read_text(encoding="utf-8").splitlines()]


def _apply(tmp_path, hist, *, op, section="self", target="", text="", turn=1,
           external_ctx=False, max_chars=1200):
    return self_profile.apply(
        tmp_path / "self_profile.md", section=section, op=op, target=target,
        text=text, max_chars=max_chars, today="2026-07-02",
        history_path=hist, turn=turn, external_ctx=external_ctx)


def test_add_logs_pin_add_with_turn_and_ctx(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    _apply(tmp_path, hist, op="add", text="喜歡看監控數字", turn=7, external_ctx=True)
    (ev,) = _events(hist)
    assert ev["kind"] == "pin_add" and ev["turn"] == 7 and ev["external_ctx"] is True
    assert ev["section"] == "self" and ev["id"] == "s1" and ev["text"] == "喜歡看監控數字"


def test_replace_logs_tombstone_old_text(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    _apply(tmp_path, hist, op="add", text="舊的我", turn=1)
    _apply(tmp_path, hist, op="replace", target="s1", text="新的我", turn=2)
    add, rep = _events(hist)
    assert rep["kind"] == "pin_replace" and rep["old_text"] == "舊的我" and rep["text"] == "新的我"


def test_remove_logs_tombstone(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    _apply(tmp_path, hist, op="add", text="被淘汰的自我", turn=1)
    _apply(tmp_path, hist, op="remove", target="s1", turn=3)
    _, rm = _events(hist)
    assert rm["kind"] == "pin_remove" and rm["old_text"] == "被淘汰的自我"


def test_same_turn_dedup_hit_logs_nothing(tmp_path):
    """Refeed artifact: re-emitted identical pin in the SAME outer turn."""
    hist = tmp_path / "self_history.jsonl"
    _apply(tmp_path, hist, op="add", text="A", turn=5)
    result = _apply(tmp_path, hist, op="add", text="A", turn=5)
    assert "已有相同條目" in result
    assert [e["kind"] for e in _events(hist)] == ["pin_add"]  # no reconfirm


def test_cross_turn_dedup_hit_logs_reconfirm(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    _apply(tmp_path, hist, op="add", text="A", turn=5)
    _apply(tmp_path, hist, op="add", text="A", turn=35)
    kinds = [e["kind"] for e in _events(hist)]
    assert kinds == ["pin_add", "pin_reconfirm"]
    assert _events(hist)[1]["turn"] == 35


def test_reconfirm_chain_is_cross_turn_per_last_event(tmp_path):
    """Second reconfirm in the same turn as the first reconfirm → not logged."""
    hist = tmp_path / "self_history.jsonl"
    _apply(tmp_path, hist, op="add", text="A", turn=5)
    _apply(tmp_path, hist, op="add", text="A", turn=35)
    _apply(tmp_path, hist, op="add", text="A", turn=35)  # refeed echo of the reconfirm turn
    assert [e["kind"] for e in _events(hist)] == ["pin_add", "pin_reconfirm"]


def test_cap_rejected_add_logs_nothing(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    result = _apply(tmp_path, hist, op="add", text="X" * 100, max_chars=30)
    assert "已達上限" in result
    assert _events(hist) == []


def test_history_path_none_is_backcompat_noop(tmp_path):
    result = self_profile.apply(
        tmp_path / "self_profile.md", section="self", op="add", target="",
        text="A", max_chars=1200, today="2026-07-02")
    assert "已 pin" in result


def test_log_io_error_swallowed_pin_still_succeeds(tmp_path, monkeypatch):
    def boom(path, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(self_history, "log_event", boom)
    hist = tmp_path / "self_history.jsonl"
    result = _apply(tmp_path, hist, op="add", text="A")
    assert "已 pin" in result  # friendly result unchanged
    assert (tmp_path / "self_profile.md").exists()  # the pin itself landed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_self_profile_history.py -v`
Expected: FAIL — `TypeError: apply() got an unexpected keyword argument 'history_path'`

- [ ] **Step 3: Modify `self_profile.py`**

At the top of the module add (after existing imports):

```python
import logging

from dollos.mind import self_history

logger = logging.getLogger(__name__)
```

Add the swallow helper (below `_entries_listing`):

```python
def _log_pin(history_path: Path | None, **fields) -> None:
    """Pin-event logging is best-effort: an IO error must never break the pin
    itself (spec §3.2 — swallow rule is pins-only). Logged loudly."""
    if history_path is None:
        return
    try:
        self_history.log_event(history_path, **fields)
    except OSError:
        logger.exception("self_history write failed (pin already applied)")
```

Change `apply`'s signature and wire the log calls (full replacement of the function; unchanged logic marked):

```python
def apply(path: Path, *, section: str, op: str, target: str, text: str,
          max_chars: int, today: str, history_path: Path | None = None,
          turn: int = 0, external_ctx: bool = False) -> str:
    """Read-modify-write self_profile.md. Returns a human-readable result or a
    friendly-error string (never raises for cap/locate misses). When
    ``history_path`` is set, successful mutations append evidence events
    (spec 2026-07-02 §3.2): tombstones on replace/remove, cross-turn-only
    reconfirm on the idempotent-add dedup hit."""
    raw = path.read_text() if path.exists() else ""
    sections = _parse(raw)
    pending_log: dict | None = None  # logged only AFTER a successful write

    if op == "add":
        if section not in SECTION_ORDER:
            return f"未知 section:{section}"
        clean_text = _strip_incoming_tag(text)
        existing = next(
            (b for b in sections[section] if b.text == clean_text), None
        )
        if existing is not None:
            # Idempotent dedup hit (comment block unchanged). Cross-turn only:
            # same-turn hits are refeed echoes and must not fabricate
            # reinforcement evidence (spec §3.2).
            if history_path is not None:
                prev = self_history.last_pin_turn(
                    history_path, section=section, text=clean_text)
                if prev is None or prev != turn:
                    _log_pin(history_path, kind="pin_reconfirm", turn=turn,
                             external_ctx=external_ctx, section=section,
                             id=existing.id, text=clean_text)
            return f"已有相同條目:{existing.id}(未重複新增)"
        new_id = _next_id(sections[section], section)
        sections[section].append(Bullet(id=new_id, date=today, text=clean_text))
        result = f"已 pin 到「{SECTION_TITLES[section]}」:{new_id}"
        pending_log = dict(kind="pin_add", turn=turn, external_ctx=external_ctx,
                           section=section, id=new_id, text=clean_text)
    elif op == "replace":
        found = _find(sections, target)
        if found is None:
            return (f"找不到符合「{target}」的條目;現有:{_entries_listing(sections)}。"
                     f"可用 id(如 s1)或貼該條目文字重試。")
        key, i = found
        old = sections[key][i]
        clean_text = _strip_incoming_tag(text)
        sections[key][i] = Bullet(id=old.id, date=today, text=clean_text)
        result = f"已更新 {old.id}"
        pending_log = dict(kind="pin_replace", turn=turn, external_ctx=external_ctx,
                           section=key, id=old.id, text=clean_text, old_text=old.text)
    elif op == "remove":
        found = _find(sections, target)
        if found is None:
            return (f"找不到符合「{target}」的條目;現有:{_entries_listing(sections)}。"
                     f"可用 id(如 s1)或貼該條目文字重試。")
        key, i = found
        old = sections[key].pop(i)
        result = f"已移除 {old.id}"
        pending_log = dict(kind="pin_remove", turn=turn, external_ctx=external_ctx,
                           section=key, id=old.id, text="", old_text=old.text)
    else:
        return f"未知 op:{op}"

    serialized = _serialize(sections)
    if op in ("add", "replace") and len(serialized) > max_chars:
        return (f"self-profile 已達上限({max_chars} 字),寫入後會是 {len(serialized)} 字。"
                f"先 remove/replace 一些再 pin。")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized)
    if pending_log is not None:
        _log_pin(history_path, **pending_log)
    return result
```

Notes for the implementer: the dedup-hit comment block currently at lines 143-148 stays where it is; `replace`/`remove` now bind `old = sections[key][i]` / `pop(i)` instead of only the id — behavior identical, tombstone captured. `pin_reconfirm` cross-turn comparison is per the LAST logged add/reconfirm of that text (see `test_reconfirm_chain_is_cross_turn_per_last_event`).

- [ ] **Step 4: Run new tests AND the existing self_profile suite**

Run: `uv run pytest tests/test_self_profile_history.py tests/test_pin_self.py tests/test_self_profile.py -v` (if `tests/test_self_profile.py` does not exist under that name, run `uv run pytest -k "self_profile or pin_self" -v`)
Expected: all PASS (back-compat: existing callers pass no history_path)

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/self_profile.py tests/test_self_profile_history.py
git commit -m "feat(mind): pin-event logging with tombstones + cross-turn reconfirm (evolution spec §3.2)"
```

---

### Task 3: turn/external_ctx threading (MindCtx → MindLoop → PinSelf)

**Files:**
- Modify: `src/dollos/mind/mind_ctx.py` (MindCtx dataclass, after `self_profile_max_chars` at line ~51)
- Modify: `src/dollos/mind/mind_loop.py` (module-level helper + `iterate()` after the `_is_reflection` line 192 + Recall upgrade in the refeed results loop ~lines 560-590)
- Modify: `src/dollos/tools.py` (`PinSelf.run`, lines 797-812)
- Test: `tests/test_evidence_threading.py`

**Interfaces:**
- Consumes: `apply(..., history_path=, turn=, external_ctx=)` (Task 2).
- Produces: `MindCtx.current_turn: int` (= `MindState.iter_count` at drain), `MindCtx.external_ctx: bool`; module function `mind_loop.batch_external(perceptions) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_evidence_threading.py
"""turn/external_ctx threading: MindLoop batch → MindCtx → PinSelf → self_history."""
import json
import time
import types

import pytest

from dollos.mind.mind_loop import batch_external
from dollos.mind.mind_state import Perception
from dollos.tools import PinSelf


def _p(kind):
    return Perception(kind=kind, t=time.time(), data={})


def test_batch_external_true_for_tool_results():
    assert batch_external([_p("UserSpoke"), _p("ToolResultArrived")]) is True
    assert batch_external([_p("MonitorFired")]) is True
    assert batch_external([_p("MonitorEnded")]) is True


def test_batch_external_false_for_internal_kinds():
    assert batch_external([_p("UserSpoke"), _p("ReflectionMoment")]) is False
    assert batch_external([]) is False


@pytest.mark.asyncio
async def test_pinself_threads_turn_and_ctx_into_history(tmp_path):
    ctx = types.SimpleNamespace(
        memory_root=tmp_path, self_profile_max_chars=1200,
        current_turn=42, external_ctx=True,
        mind_state=types.SimpleNamespace(recent_outputs=[]),
    )
    tool = PinSelf(section="self", op="add", target="", text="喜歡監控數字")
    result = await tool.run(ctx)
    assert "已 pin" in result
    hist = tmp_path / "self_history.jsonl"
    (ev,) = [json.loads(l) for l in hist.read_text(encoding="utf-8").splitlines()]
    assert ev["turn"] == 42 and ev["external_ctx"] is True and ev["kind"] == "pin_add"
```

Note: if `PinSelf.run`'s `_record(ctx, ...)` needs more of MindCtx than the SimpleNamespace provides, extend the namespace to match — copy the stub pattern already used in `tests/test_pin_self.py` (which stubs MindCtx for `PinSelf.run` today) instead of inventing a new one.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_evidence_threading.py -v`
Expected: FAIL — `ImportError: cannot import name 'batch_external'`

- [ ] **Step 3: Implement the threading**

`src/dollos/mind/mind_ctx.py` — add after `self_profile_max_chars: int = 1200`:

```python
    # 慢變演化 evidence layer (spec 2026-07-02 §3.2): per-turn provenance,
    # set by MindLoop at drain time; Recall execution upgrades external_ctx
    # mid-cascade. Threaded into PinSelf → self_history.
    current_turn: int = 0
    external_ctx: bool = False
```

`src/dollos/mind/mind_loop.py` — module level (near `IN_TURN_REFEED_TOOLS`, line ~66):

```python
# Perception kinds that mean "this turn's context contains externally-sourced
# content" (spec 2026-07-02 §3.2). [Memory context] auto-injection deliberately
# does NOT count — it is present on almost every turn and would saturate the flag.
_EXTERNAL_KINDS = frozenset({"ToolResultArrived", "MonitorFired", "MonitorEnded"})


def batch_external(perceptions) -> bool:
    """True when the drained batch carries external content (provenance flag)."""
    return any(p.kind in _EXTERNAL_KINDS for p in perceptions)
```

In `iterate()`, directly after `self._is_reflection = any(...)` (line 192):

```python
        # Evidence-layer provenance (spec §3.2): one turn value shared by all
        # cascade/refeed passes of this iteration.
        self._ctx.current_turn = self._state.iter_count
        self._ctx.external_ctx = batch_external(perceptions)
```

In the refeed results loop (lines ~560-590, where `r.tool_name` is inspected against `IN_TURN_REFEED_TOOLS`), add alongside that inspection:

```python
                    # Recall output is in-context external content from here on
                    # (spec §3.2 — Recall never appears in the drained batch).
                    if r.tool_name == "Recall" and r.success:
                        self._ctx.external_ctx = True
```

Placement rule: it must run for every executed batch of tool results BEFORE the next refeed pass dispatches, so a PinSelf emitted after a Recall in the same cascade sees `external_ctx=True`.

`src/dollos/tools.py` — in `PinSelf.run` (line ~801), pass the new kwargs:

```python
        result = self_profile.apply(
            path,
            section=self.section,
            op=self.op,
            target=self.target,
            text=self.text,
            max_chars=ctx.self_profile_max_chars,
            today=today,
            history_path=ctx.memory_root / "self_history.jsonl",
            turn=ctx.current_turn,
            external_ctx=ctx.external_ctx,
        )
```

- [ ] **Step 4: Run the new tests, then the full suite**

Run: `uv run pytest tests/test_evidence_threading.py -v`
Expected: PASS
Run: `uv run pytest`
Expected: all green (873 existing + 16 new = 889; exact count may differ — no failures is the criterion)

- [ ] **Step 5: Structural guard — history file never indexed**

Append to `tests/test_self_history.py` (mirror of self_profile's structural test — find it via `grep -rn "memory_root" tests/ | grep -i "index\|_paths"` and follow its exact assertion style):

```python
def test_self_history_lives_outside_index_paths():
    """self_history.jsonl sits at memory_root root — FtsMemory only indexes
    [shared, transcripts, skills] subtrees, so it can never enter recall.
    Structural guard mirroring self_profile.md's (spec §3.2)."""
    from dollos.tools import PinSelf  # the only writer wires the path
    import inspect
    src = inspect.getsource(PinSelf.run)
    assert 'memory_root / "self_history.jsonl"' in src
    assert "index_file" not in src
```

Run: `uv run pytest tests/test_self_history.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/dollos/mind/mind_ctx.py src/dollos/mind/mind_loop.py src/dollos/tools.py tests/test_evidence_threading.py tests/test_self_history.py
git commit -m "feat(mind): thread turn/external_ctx provenance into pin events (evolution spec §3.2)"
```

---

## Completion

After Task 3: full suite green, then merge via `superpowers:finishing-a-development-branch`. This plan is a complete standalone feature (closes the PinSelf guidance spec §5 tombstone debt) — evidence starts accumulating for the keeper the moment it merges, weeks before Plan 3 reads it. Plans 2 (artifact + ratification + SelfRevision) and 3 (evolution pass) are written after this one lands, per the incremental-planning house rule.
