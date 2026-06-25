# Plan: Core-Loop Robustness (P5 → P10 → P2 → P1 → P6)

> **For agentic workers:** REQUIRED SUB-SKILL: use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax. One concept per task. TDD: failing test first.

**Spec:** `docs/superpowers/specs/2026-06-25-core-loop-robustness-design.md`
(read it before starting — every task is grounded there).

**Goal:** harden Doll's always-on turn loop. Fix two silent-data-loss bugs,
delete a dead event vocabulary, reorder the think grammar, capture the discarded
REVIEW, give the live turn an in-turn sync-tool re-feed (without breaking voice
streaming), and bound the resulting cascade with a tool-existence guard +
read-only safe-mode.

**Sequencing rationale (safest-first):** P5 (data integrity, no behaviour change)
→ P10 (hygiene on the now-safe state layer) → P2-grammar (zero-cost) → P2-capture
(additive field) → P1 (structural, needs P6.1) → P6 (bounds the new cascade).
P1 must NOT ship without P6 Task 9 (tool-existence guard) + P1 Task 8 (budget
cap).

**Tech stack:** Python 3.13, asyncio, pydantic, GBNF via llama.cpp, structlog,
pytest. No new external dependency.

**Out of scope (separate tracks — see spec §OS / §DEF):** P3 episodic capture,
P4 sleep-time consolidation, P9 inspectable subagents, P7 goal actuation, P8
persona hardening, and any inline verifier turn.

**Run tests:** `uv run pytest --ignore=tests/voice -q` (green, or only the known
pre-existing milvus failure). Grammar/parser/state tests run without an LLM.

---

## Task 1 — P5.1: field-tolerant `load_state` + surface-not-blank on corruption

**Files:**
- Modify: `src/dollos/mind/mind_state.py`
- Create: `tests/test_mind_state_durability.py`

- [ ] **Step 1: Failing tests** (`tests/test_mind_state_durability.py`)

```python
import json
import time
from pathlib import Path

import pytest

from dollos.mind.mind_state import (
    MindState, Mood, Perception, load_state, save_state,
)
# New symbol introduced by this task:
from dollos.mind.mind_state import MindStateLoadError


def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def test_additive_top_level_field_preserved_not_blanked(tmp_path):
    """A future additive top-level key must NOT blank the whole self."""
    p = tmp_path / "mind_state.json"
    s = MindState(mood=Mood(emotion="開心", reason="x"), scratchpad="keep me")
    save_state(s, p)
    data = json.loads(p.read_text())
    data["some_future_field"] = 123          # additive drift
    _write(p, data)

    loaded = load_state(p)
    assert loaded.scratchpad == "keep me"     # not blanked
    assert loaded.mood.emotion == "開心"


def test_additive_nested_field_preserved(tmp_path):
    """An extra key inside a nested record must be tolerated, record kept."""
    p = tmp_path / "mind_state.json"
    s = MindState()
    s.recent_perceptions.append(
        Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"})
    )
    save_state(s, p)
    data = json.loads(p.read_text())
    data["recent_perceptions"][0]["future_inner_field"] = "x"   # nested drift
    _write(p, data)

    loaded = load_state(p)
    assert len(loaded.recent_perceptions) == 1
    assert loaded.recent_perceptions[0].data == {"text": "hi"}


def test_missing_file_is_cold_start(tmp_path):
    loaded = load_state(tmp_path / "nope.json")
    assert isinstance(loaded, MindState)
    assert loaded.scratchpad == ""


def test_corrupt_json_raises_and_quarantines(tmp_path):
    """Genuine corruption surfaces (raises) + quarantines — never blanks."""
    p = tmp_path / "mind_state.json"
    p.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(MindStateLoadError):
        load_state(p)
    quarantined = list(tmp_path.glob("mind_state.json.corrupt-*"))
    assert quarantined, "corrupt file must be quarantined, not left in place"
```

- [ ] **Step 2: Run failing** — `uv run pytest tests/test_mind_state_durability.py -v`
  (expect `ImportError: MindStateLoadError` then assertion failures).

- [ ] **Step 3: Implement** in `src/dollos/mind/mind_state.py`:
  - Add `class MindStateLoadError(Exception)`.
  - Add a small helper `def _coerce(cls, d: dict)` that filters `d` to
    `cls.__dataclass_fields__` keys before `cls(**filtered)` (field-tolerant).
    Use it for `Mood`, `ActiveTask`, `PendingEvent`, `OpenLoop`, `Perception`,
    `OutputRecord`.
  - `load_state`: keep the missing-file → `MindState()` branch. On
    `json.JSONDecodeError` OR any reconstruction `Exception`: **quarantine**
    (`path.rename(path.with_name(path.name + f".corrupt-{int(time.time())}"))`)
    and `raise MindStateLoadError(...)` naming the quarantine path. Remove BOTH
    blank-reset `return MindState()` paths (lines 135-137 and 172-174).
  - Top-level scalar fields keep `data.get(...)` defaults (already tolerant).

- [ ] **Step 4: Pass** — `uv run pytest tests/test_mind_state_durability.py -v`.

- [ ] **Step 5: Kernel propagation check.** Confirm `kernel.py:212`'s
  `load_state(...)` is NOT wrapped in a swallowing try/except; on
  `MindStateLoadError` the daemon must abort startup with the message. Add a
  log line naming the quarantine path if helpful. (No test required if no guard
  exists; otherwise add a kernel test that the exception propagates.)

- [ ] **Step 6: Commit**
```bash
git add src/dollos/mind/mind_state.py tests/test_mind_state_durability.py src/dollos/kernel.py
git commit -m "fix(mind_state): field-tolerant load + surface-not-blank on corruption (P5.1)"
```

---

## Task 2 — P5.2: `save_state` returns bool; gate WAL truncation on durable save

**Files:**
- Modify: `src/dollos/mind/mind_state.py` (`save_state`)
- Modify: `src/dollos/mind/mind_loop.py` (`iterate`)
- Extend: `tests/test_mind_state_durability.py`, `tests/test_mind_loop.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_mind_state_durability.py — append
def test_save_state_returns_true_on_success(tmp_path):
    from dollos.mind.mind_state import MindState, save_state
    assert save_state(MindState(), tmp_path / "s.json") is True


def test_save_state_returns_false_on_failure(tmp_path):
    from dollos.mind.mind_state import MindState, save_state
    # Parent is a file, so mkdir/open fails -> save returns False, does not raise.
    bad = tmp_path / "afile"
    bad.write_text("x")
    assert save_state(MindState(), bad / "s.json") is False
```

```python
# tests/test_mind_loop.py — append (mirror of the crash-recovery green-path test)
@pytest.mark.asyncio
async def test_iterate_does_not_truncate_wal_when_save_fails(tmp_path, monkeypatch):
    from dollos.wal.perception_log import PerceptionWAL
    import dollos.mind.mind_loop as ml
    wal = PerceptionWAL(tmp_path / "wal.jsonl")
    queue = PerceptionQueue(wal=wal)
    queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"}))

    monkeypatch.setattr(ml, "save_state", lambda *a, **k: False)  # force failed save
    loop = _make_mind_loop(tmp_path, queue=queue, wal=wal)
    await loop.iterate()

    assert list(wal.iter_pending()) != []   # perceptions survive a failed save
```

- [ ] **Step 2: Run failing**.

- [ ] **Step 3: Implement**:
  - `save_state`: annotate `-> bool`; `return True` after the successful
    `tmp_path.replace(path)`; in the `except`, after cleanup/log, `return False`.
  - `iterate` (`mind_loop.py:151-164`): `saved = save_state(self._state, ...)`;
    wrap the WAL-truncation block in `if self._wal is not None and perceptions
    and saved:`; else log `warning("save failed; skipping WAL truncation to
    preserve perceptions for replay")`.

- [ ] **Step 4: Pass** — run both test files; then the durability smoke gate
  from the spec is now complete (Task 1 + Task 2 together).

- [ ] **Step 5: Commit**
```bash
git add src/dollos/mind/mind_state.py src/dollos/mind/mind_loop.py tests/test_mind_state_durability.py tests/test_mind_loop.py
git commit -m "fix(mind_loop): gate WAL truncation on durable save; save_state returns bool (P5.2)"
```

---

## Task 3 — P10: delete dead `events.py` + sync `Perception.kind` Literal

**Files:**
- Delete: `src/dollos/events.py`
- Modify: `src/dollos/mind/mind_state.py` (`Perception.kind` Literal)
- Modify/remove: any test importing `dollos.events`
- Create/extend: `tests/test_perception_kind.py`

- [ ] **Step 1: Sweep** — `grep -rn "dollos.events\|from dollos import events" src tests`.
  Record every hit (expected: none in `src/`; possibly a stale test).

- [ ] **Step 2: Failing test** (`tests/test_perception_kind.py`)

```python
from typing import get_args
from dollos.mind.mind_state import Perception


def test_interrupted_in_kind_literal():
    kinds = get_args(Perception.__dataclass_fields__["kind"].type)
    assert "Interrupted" in kinds  # kernel.py emits kind="Interrupted"
```

(If `.type` is a string under `from __future__ import annotations`, resolve via
`typing.get_type_hints(Perception)` instead; the assertion intent is unchanged.)

- [ ] **Step 3: Implement**
  - Add `"Interrupted"` to the `Perception.kind` `Literal[...]`
    (`mind_state.py:52-55`). Audit other `kind=` strings produced in `src/`
    against the Literal; add any other missing member found.
  - `rm src/dollos/events.py`. Remove any dead import found in Step 1; delete or
    adjust any test that only existed to test `events.py`.

- [ ] **Step 4: Pass** — `uv run pytest tests/test_perception_kind.py -v` then the
  full suite (`uv run pytest --ignore=tests/voice -q`) to prove nothing depended
  on `events.py`.

- [ ] **Step 5: Commit**
```bash
git add -A
git commit -m "chore(events): delete dead two-tier event vocabulary; add Interrupted to Perception.kind (P10)"
```

---

## Task 4 — P2-grammar: reorder voice-first think so TOOL precedes REVIEW

**Files:**
- Modify: `src/dollos/llm/templates.py` (`build_voice_first_grammar`)
- Extend: the templates test (e.g. `tests/test_templates.py` — match the existing
  grammar test file name)

- [ ] **Step 1: Failing test** — string-assertion, house style:

```python
def test_voice_first_grammar_tool_precedes_review():
    from dollos.llm.templates import build_voice_first_grammar
    from dollos.tools import MAIN_TOOLS
    g = build_voice_first_grammar(MAIN_TOOLS)
    # New order: INTENT -> TOOL -> REVIEW -> MOOD
    assert '"INTENT: " line "TOOL: " line "REVIEW: "' in g
    # Old order must be gone:
    assert '"REVIEW: " line "MOOD: " line "TOOL: "' not in g


def test_voice_first_grammar_tail_unchanged():
    from dollos.llm.templates import build_voice_first_grammar
    from dollos.tools import MAIN_TOOLS
    g = build_voice_first_grammar(MAIN_TOOLS)
    assert "segments ::= segment*" in g
    assert "segment ::= speak | tool-call" in g
    assert "speak ::= [^<]+" in g
```

- [ ] **Step 2: Run failing**.

- [ ] **Step 3: Implement** — in `build_voice_first_grammar` change the `think`
  rule (`templates.py:341-342`) to:
```
'think ::= "SEEN: " line "INTENT: " line "TOOL: " line '
'"REVIEW: " line "MOOD: " line "</think>\\n\\n"\n'
```
  Touch nothing else (do not touch `build_qwen3_think_tool_grammar`).

- [ ] **Step 4: Pass** — templates tests + existing voice-parser tests
  (`tests/test_tool_parser.py`) green unchanged (think still stripped to
  `</think>`).

- [ ] **Step 5: Commit**
```bash
git add src/dollos/llm/templates.py tests/test_templates.py
git commit -m "feat(grammar): voice-first think emits TOOL before REVIEW (post-hoc critique) (P2-grammar)"
```

---

## Task 5 — P2-capture: parse REVIEW into MindState + surface recent REVIEWs

**Files:**
- Modify: `src/dollos/mind/mind_state.py` (new `recent_reviews` deque + ser/de)
- Modify: `src/dollos/mind/mind_loop.py` (accumulate raw emit, parse, persist)
- Modify: `src/dollos/mind/mind_prompt.py` (`[Recent self-review]` block)
- Reuse: `src/dollos/cascade_log.py::_parse_think`
- Extend: `tests/test_mind_loop.py`, `tests/test_mind_prompt.py`,
  `tests/test_mind_state_durability.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_mind_loop.py — append
@pytest.mark.asyncio
async def test_review_captured_into_state(tmp_path):
    # Scripted stream whose think block carries a REVIEW line.
    text = "SEEN: x\nINTENT: y\nTOOL: none\nREVIEW: I should not have repeated myself\nMOOD: 平靜\n</think>\n\n好的"
    loop = _make_mind_loop(tmp_path, scripted_stream=[text])  # adapt helper
    await loop.iterate_once_with(text)   # or drive via a UserSpoke perception
    reviews = list(loop._state.recent_reviews)
    assert reviews and "repeated myself" in reviews[-1]


# tests/test_mind_prompt.py — append
def test_recent_self_review_block_rendered():
    from dollos.mind.mind_state import MindState
    from dollos.mind.mind_prompt import render_mind
    s = MindState()
    s.recent_reviews.append("did not check the file first")
    out = render_mind(s, [], "SYS")
    assert "[Recent self-review]" in out
    assert "did not check the file first" in out


# tests/test_mind_state_durability.py — append
def test_recent_reviews_round_trips(tmp_path):
    from dollos.mind.mind_state import MindState, load_state, save_state
    s = MindState(); s.recent_reviews.append("lesson A")
    p = tmp_path / "s.json"; save_state(s, p)
    assert "lesson A" in list(load_state(p).recent_reviews)
```

(Adapt `_make_mind_loop` / the drive helper to the existing `tests/test_mind_loop.py`
fixtures; the assertion intent is what matters.)

- [ ] **Step 2: Run failing**.

- [ ] **Step 3: Implement**
  - `mind_state.py`: add `recent_reviews: deque[str] = field(default_factory=lambda:
    deque(maxlen=5))`. Serialise it in `save_state` (`list(...)`) and reconstruct
    in `load_state` (`deque(data.get("recent_reviews", []), maxlen=5)`) — under
    Task 1's tolerant path.
  - `mind_loop.py`: in `_llm_iterate`, accumulate `chunk.text` into a
    `raw_buf: list[str]` (alongside the existing parse). After the stream/flush,
    `from dollos.cascade_log import _parse_think`; `fields = _parse_think("".join(raw_buf))`;
    if `fields.get("review")`: `self._state.recent_reviews.append(fields["review"])`.
    (MOOD is parsed too but NOT written to `state.mood` — see spec §6.2 / §WRONG.4.)
  - `mind_prompt.py`: render `[Recent self-review]` from `state.recent_reviews`
    (oldest→newest, each line truncated), placed near the other self-state blocks.
    Omit the block entirely when empty.

- [ ] **Step 4: Pass** — the three test files green.

- [ ] **Step 5 (optional): wire CascadeLogger.** If included as its own commit:
  in `_llm_iterate`, after parsing, call the kernel-provided `CascadeLogger.log_iter`
  (thread the logger into `MindLoop.__init__`). Gate inclusion on "adds no decode";
  drop if it grows scope. Add a test that `log_iter` is invoked once per turn.

- [ ] **Step 6: Commit**
```bash
git add src/dollos/mind/mind_state.py src/dollos/mind/mind_loop.py src/dollos/mind/mind_prompt.py tests/
git commit -m "feat(metacognition): capture REVIEW into recent_reviews + surface [Recent self-review] (P2-capture)"
```

---

## Task 6 — P6.1: post-decode tool-existence/args guard re-enters as error (PREREQUISITE for P1)

> Land this BEFORE the P1 cascade wiring (Task 7). It makes an invalid call a
> grounded error `<tool_response>` instead of a silent no-op.

**Files:**
- Modify: `src/dollos/mind/mind_loop.py` (`_dispatch_tool` → return `ToolResult | None`)
- Reuse: `src/dollos/cascade/tool_loop.py::ToolResult` (do not invent a new type)
- Extend: `tests/test_mind_loop.py`

- [ ] **Step 1: Failing test**

```python
@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_failed_result(tmp_path):
    from dollos.cascade.tool_loop import ToolResult
    loop = _make_mind_loop(tmp_path)
    r = await loop._dispatch_tool("NoSuchTool", {})
    assert isinstance(r, ToolResult) and r.success is False
    assert "unknown" in r.detail.lower()


@pytest.mark.asyncio
async def test_dispatch_bad_args_returns_failed_result(tmp_path):
    from dollos.cascade.tool_loop import ToolResult
    loop = _make_mind_loop(tmp_path)
    r = await loop._dispatch_tool("Recall", {})   # missing required 'query'
    assert isinstance(r, ToolResult) and r.success is False
```

- [ ] **Step 2: Run failing**.

- [ ] **Step 3: Implement** — change `_dispatch_tool` (`mind_loop.py:268-281`) to
  return `ToolResult | None`, mirroring `dispatch_tool_call` semantics:
  - unknown name → `ToolResult(name, success=False, detail="unknown tool")`
  - `ValidationError` → `ToolResult(..., success=False, detail=f"args validation: {e}")`
  - runtime exception → `ToolResult(..., success=False, detail=f"runtime error: {e}")`
  - success: `returned = await tool.run(ctx)`; `None` → return `None`
    (side-effect / fire-and-forget tool); str → `ToolResult(..., success=True, detail=returned)`.
  `_handle_stream_event` updated to receive and return this result up the call
  chain (consumed in Task 7).

- [ ] **Step 4: Pass**. (No behaviour change yet for users — results are produced
  but not yet re-fed; that is Task 7.)

- [ ] **Step 5: Commit**
```bash
git add src/dollos/mind/mind_loop.py tests/test_mind_loop.py
git commit -m "feat(mind_loop): _dispatch_tool returns ToolResult incl. existence/args guard (P6.1)"
```

---

## Task 7 — P1: in-turn sync-tool re-feed on the streaming path (+ budget cap)

> Depends on Task 6 (results captured) and ships WITH the budget cap below so the
> new live cascade can never run unbounded.

**Files:**
- Modify: `src/dollos/mind/mind_loop.py` (`_llm_iterate` → streaming cascade)
- Modify: `src/dollos/kernel.py` (give MindLoop a messages-capable adapter)
- Extend: `tests/test_mind_loop.py`

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.asyncio
async def test_recall_result_refed_in_same_turn(tmp_path):
    """pass 1 emits a Recall tool call -> a 2nd pass runs whose input carries the
    Recall result as <tool_response>; both passes' speech reach the sink in order."""
    # Script a 2-pass stream: pass1 = think + <tool_call> Recall; pass2 = think + speech.
    loop, sink, captured_messages = _make_mind_loop_capturing(tmp_path, script=[...])
    await loop.iterate()  # driven by a UserSpoke perception
    joined = "\n".join(m["content"] for m in captured_messages if m["role"] == "user")
    assert "<tool_response>" in joined


@pytest.mark.asyncio
async def test_fire_and_forget_tool_runs_single_pass(tmp_path):
    """A turn whose only tool is async Shell does NOT trigger a 2nd decode."""
    loop, _, pass_counter = _make_mind_loop_counting(tmp_path, script=[shell_pass])
    await loop.iterate()
    assert pass_counter.value == 1


@pytest.mark.asyncio
async def test_pure_speech_turn_single_pass(tmp_path):
    loop, _, pass_counter = _make_mind_loop_counting(tmp_path, script=[speech_only])
    await loop.iterate()
    assert pass_counter.value == 1


@pytest.mark.asyncio
async def test_cancel_during_second_pass_exits_clean(tmp_path):
    # Cancel between pass 1 and pass 2; assert no exception, no pass-2 speech.
    ...


@pytest.mark.asyncio
async def test_sync_tool_budget_cap_terminates(tmp_path):
    """A model that keeps emitting sync tools terminates at the budget cap."""
    loop, _, pass_counter = _make_mind_loop_counting(tmp_path, script=[recall_loop]*20)
    await loop.iterate()
    assert pass_counter.value <= 8  # MAX_SYNC_REFEED_PASSES
```

- [ ] **Step 2: Run failing**.

- [ ] **Step 3: Implement** in `mind_loop.py` — restructure `_llm_iterate` into an
  outer cascade loop (spec §7.1):
  - `self._cascade_ctx = CascadeCtx()` set ONCE at turn start, cleared in
    `finally` (cancellation spans all passes).
  - `messages: list[dict] = [{"role": "user", "content": prompt}]`.
  - `MAX_SYNC_REFEED_PASSES = 8` (module constant). Loop while passes remain:
    - run ONE streaming pass with the EXISTING voice machinery
      (`ToolStreamParser(voice_mode=True)` + `SentenceChunker` + live sink),
      accumulating `raw_buf` (also used by Task 5) and collecting the non-`None`
      `ToolResult`s from `_dispatch_tool`.
    - append `{"role": "assistant", "content": "".join(raw_buf)}`.
    - if no results → break.
    - port the same-tool 3-strike abort from `tool_loop.py:189-219` (break + log).
    - else append one `{"role": "user", "content":
      f"<tool_response>\n{r.detail or '(no output)'}\n</tool_response>"}` per
      result; check cancel; loop. Enforce `MAX_SYNC_REFEED_PASSES`.
  - Pass 1 uses the existing `stream_completion(user=prompt)`; pass ≥ 2 uses a
    messages-capable stream (Task 7 Step 4). Cancel-checks at each pass boundary
    and before each re-feed (reuse the existing checks at `mind_loop.py:218-238`).

- [ ] **Step 4: Adapter plumbing** in `kernel.py` — widen `_MindLLMAdapter`
  (`kernel.py:152-181`) to ALSO expose `stream_messages(system, messages, ...)`
  delegating to the underlying `LLMAdapter.stream_messages`; pass it into
  `MindLoop`. `MindLoop` calls `stream_completion` for pass 1 and `stream_messages`
  for pass ≥ 2 (rendered by `Qwen3ThinkingTemplate.render_messages`, which already
  emits the right alternation). Add a templates-equivalence test if you instead
  unify both passes onto `stream_messages`.

- [ ] **Step 5: Pass** — all Task 7 tests + the existing voice-parser/voice
  tests green. Manually re-read the regression assertions: pure-speech and
  fire-and-forget turns are single-pass (latency unchanged).

- [ ] **Step 6: Commit**
```bash
git add src/dollos/mind/mind_loop.py src/dollos/kernel.py tests/test_mind_loop.py
git commit -m "feat(mind_loop): in-turn sync-tool re-feed (streaming cascade) + budget cap (P1)"
```

---

## Task 8 — P6.3: read-only safe-mode (bounded-severity, announced)

**Files:**
- Modify: `src/dollos/mind/mind_state.py` (`safe_mode` flag + reason)
- Modify: `src/dollos/mind/mind_loop.py` (trigger + reduced tool set per pass)
- Modify: `src/dollos/mind/mind_prompt.py` (`[Safe mode]` banner)
- Modify: `src/dollos/kernel.py` (enqueue help perception on entry)
- Extend: `tests/test_mind_loop.py`, `tests/test_mind_prompt.py`

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.asyncio
async def test_k_consecutive_failures_enter_safe_mode(tmp_path):
    loop = _make_mind_loop(tmp_path, script=[failing_tool_pass]*4)  # K=3
    await loop.iterate()
    assert loop._state.safe_mode is True


@pytest.mark.asyncio
async def test_safe_mode_excludes_write_tools(tmp_path):
    loop = _make_mind_loop(tmp_path)
    loop._state.safe_mode = True
    tools = loop._active_tool_registry()   # helper that respects safe_mode
    names = set(tools)
    assert "Recall" in names
    assert "Shell" not in names and "NoteMemory" not in names and "SpawnSubagent" not in names


def test_safe_mode_banner_rendered():
    from dollos.mind.mind_state import MindState
    from dollos.mind.mind_prompt import render_mind
    s = MindState(); s.safe_mode = True
    assert "[Safe mode]" in render_mind(s, [], "SYS")


@pytest.mark.asyncio
async def test_user_turn_clears_safe_mode(tmp_path):
    loop = _make_mind_loop(tmp_path)
    loop._state.safe_mode = True
    # drive a successful UserSpoke turn
    await loop.iterate()  # with a benign speech-only script
    assert loop._state.safe_mode is False
```

- [ ] **Step 2: Run failing**.

- [ ] **Step 3: Implement**
  - `mind_state.py`: add `safe_mode: bool = False` and `safe_mode_reason: str = ""`
    (serialised; tolerant-loaded).
  - `mind_loop.py`: track consecutive tool failures across passes; on
    `K=3` consecutive failures OR the 3-strike stuck flag, set `safe_mode=True`
    + reason, and enqueue (via the queue) a help Perception. Add
    `_active_tool_registry()` returning the full registry normally and a
    read-only subset (`Recall`, scratchpad reads; exclude `Shell`,
    `SpawnSubagent`, `SpawnMonitor`, `NoteMemory`, `WriteDiary`, `WriteSchedule`)
    when `safe_mode`. Build the per-pass grammar from that subset. Clear
    `safe_mode` at the start of a successful `UserSpoke` turn.
  - `mind_prompt.py`: render a persistent one-line `[Safe mode] <reason>` banner
    whenever `state.safe_mode` (visible EVERY turn — not edge-triggered).
  - `kernel.py`: if the help-perception is enqueued from the loop, no kernel
    change needed beyond confirming the queue path; otherwise enqueue there.

- [ ] **Step 4: Pass** — Task 8 tests green; full suite green.

- [ ] **Step 5: Commit**
```bash
git add src/dollos/mind/mind_state.py src/dollos/mind/mind_loop.py src/dollos/mind/mind_prompt.py src/dollos/kernel.py tests/
git commit -m "feat(mind_loop): bounded-severity read-only safe-mode + [Safe mode] banner (P6.3)"
```

---

## Task 9 — Full-suite gate + spec cross-check

- [ ] **Step 1: Full suite** — `uv run pytest --ignore=tests/voice -q`. Green, or
  only the known pre-existing milvus failure. If voice tests are runnable in this
  env, run `tests/voice` too (P1/P2-grammar are the voice-path-sensitive changes).

- [ ] **Step 2: Spec coverage cross-check** — confirm each spec section has a
  landed task:
  - §4 P5.1/P5.2/smoke → Tasks 1, 2
  - §5 P10 → Task 3
  - §6.1 P2-grammar → Task 4
  - §6.2 P2-capture (+§6.4 optional logger) → Task 5
  - §8.1 P6.1 guard → Task 6
  - §7 P1 (+§8.2 budget cap) → Task 7
  - §8.3 P6.3 safe-mode → Task 8
  - §6.3 verifier = design-intent only (NO task — correct, deferred to sleep-time)
  - §DEF P7/P8, §OS P3/P4/P9 = not in this plan (correct)

- [ ] **Step 3: No-fallback audit** — grep the diff for any silent
  `return MindState()` / swallowed save / blanket `except: pass` introduced;
  ensure every degradation path is either tolerant-preserve or
  surface-and-halt/announce.

- [ ] **Step 4: Commit (if any cleanup)**
```bash
git add -A
git commit -m "test(core-loop): full-suite gate + spec cross-check for core-loop-robustness"
```

---

## Self-Review Checklist

**Spec coverage:** P5 (2 bugs + smoke), P10 (delete + Literal), P2-grammar
(reorder), P2-capture (REVIEW + surface), P1 (streaming cascade + budget), P6
(existence guard + safe-mode) — all have tasks. Verifier/P7/P8/P3/P4/P9 explicitly
NOT in this plan (deferred / other tracks).

**Sequencing:** P5 → P10 → P2-grammar → P2-capture → P6.1(guard) → P1 → P6.3.
P1 (Task 7) depends on the guard (Task 6) and ships its budget cap inline.

**Type/contract consistency:** `save_state -> bool`; `_dispatch_tool ->
ToolResult | None` reuses `cascade.tool_loop.ToolResult` (no parallel type);
`recent_reviews`, `safe_mode` are additive `MindState` fields (safe under the new
tolerant `load_state`); `Perception.kind` gains `"Interrupted"`.

**Latency guards:** pass-1 path unchanged (single-prompt streaming); re-feed only
after a sync-tool call the user already waited on; P2-grammar/capture add no
decode; NO inline verifier turn.

**No-fallback:** corruption surfaces + quarantines (never blanks); failed save
skips truncation (never loses); safe-mode is announced + visible (never silent).

**Known limitations / follow-ups (out of this plan):**
- Episodic capture (P3) still dead — `recent_reviews` lives only in the in-memory
  deque + state file, not the searchable corpus. Couple to the memory track.
- Inline verifier intentionally absent — belongs to the sleep-time track.
- Embedding-trajectory loop detection intentionally NOT the cap (deterministic
  3-strike + budget is). Revisit behind a flag only after piloting.

**Plan complete.**
