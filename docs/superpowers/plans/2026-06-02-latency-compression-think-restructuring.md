# Think Restructuring (Reflex / Deliberate) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the model pick a zero-think REFLEX branch (speak immediately) vs the full SEEN/INTENT/REVIEW/MOOD/TOOL DELIBERATE branch inside the voice-first grammar, cutting per-turn latency on simple conversational turns without weakening Self-First.

**Architecture:** Add an in-grammar branch to `build_voice_first_grammar` so the model's first token chooses `REFLEX` (speak-only, no tool-call) or full think. The voice parser already discards think until `</think>`, so reflex flows through unchanged. Add a one-line prompt nudge so the model knows when to pick reflex, and tag each cascade-log row with `mode` for reflex-rate analysis. Finally an eval script measures the reflex hit-rate as a ship gate.

**Tech Stack:** Python, GBNF grammar strings, pytest, structlog, httpx (eval against the IPC/llama-server).

**Spec:** `docs/superpowers/specs/2026-06-02-latency-compression-think-restructuring-design.md`

---

## Operating notes (read before starting)

- **Worktree per plan** (project rule): create one via `superpowers:using-git-worktrees` before Task 1; merge to main via `superpowers:finishing-a-development-branch` after the last task.
- Run tests with `uv run pytest` from repo root. Baseline has one **pre-existing** unrelated failure: `test_sink_fires_tts_on_text_chunk` (mock bug on main) — ignore it; do not "fix" it as part of this plan.
- GBNF rule names may contain hyphens (the existing grammar uses `tool-call`), so `speak-only` is a valid rule name.
- The Qwen3-thinking template prefills `<think>\n`, so the grammar `root` begins right after it — the model's first emission is either `REFLEX` or `SEEN: `.
- Grammar tests in this codebase are **string-assertion** style (assert a substring is in the grammar text); they do not parse GBNF. Match that style.

---

## Task 1: Add the REFLEX / DELIBERATE branch to the voice-first grammar

**Files:**
- Modify: `src/dollos/llm/templates.py` (the `head` block inside `build_voice_first_grammar`, currently around lines 335–345)
- Test: `tests/test_llm_grammar.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_llm_grammar.py`:

```python
def test_voice_first_grammar_has_reflex_deliberate_branch():
    from dollos.tools import NoteMemory
    from dollos.llm.templates import build_voice_first_grammar
    g = build_voice_first_grammar([NoteMemory])
    assert "root ::= reflex | deliberate\n" in g
    assert 'reflex ::= "REFLEX\\n</think>\\n\\n" speak-only\n' in g
    assert "deliberate ::= think segments\n" in g


def test_voice_first_reflex_is_speak_only_no_tool_call():
    """Reflex must be speak-only: the speak-only rule is speak* with no
    tool-call alternative, so a reflex turn cannot emit a tool call."""
    from dollos.tools import NoteMemory
    from dollos.llm.templates import build_voice_first_grammar
    g = build_voice_first_grammar([NoteMemory])
    assert "speak-only ::= speak*\n" in g


def test_voice_first_deliberate_keeps_full_think_skeleton():
    """Deliberate branch is byte-for-byte the prior think structure."""
    from dollos.tools import NoteMemory
    from dollos.llm.templates import build_voice_first_grammar
    g = build_voice_first_grammar([NoteMemory])
    assert (
        'think ::= "SEEN: " line "INTENT: " line "REVIEW: " line '
        '"MOOD: " line "TOOL: " line "</think>\\n\\n"\n'
    ) in g
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_llm_grammar.py -k "reflex or deliberate" -v`
Expected: FAIL — `root ::= reflex | deliberate` not present (current grammar has `root ::= think segments`).

- [ ] **Step 3: Modify the grammar head**

In `src/dollos/llm/templates.py`, inside `build_voice_first_grammar`, replace the current `head = (...)` assignment:

```python
    head = (
        "root ::= think segments\n"
        'think ::= "SEEN: " line "INTENT: " line "REVIEW: " line '
        '"MOOD: " line "TOOL: " line "</think>\\n\\n"\n'
        'line ::= [^\\n]+ "\\n"\n'
        "segments ::= segment*\n"
        "segment ::= speak | tool-call\n"
        "speak ::= [^<]+\n"
        f"tool-call ::= {tool_call_alts}\n"
    )
```

with:

```python
    head = (
        "root ::= reflex | deliberate\n"
        'reflex ::= "REFLEX\\n</think>\\n\\n" speak-only\n'
        "deliberate ::= think segments\n"
        'think ::= "SEEN: " line "INTENT: " line "REVIEW: " line '
        '"MOOD: " line "TOOL: " line "</think>\\n\\n"\n'
        'line ::= [^\\n]+ "\\n"\n'
        "speak-only ::= speak*\n"
        "segments ::= segment*\n"
        "segment ::= speak | tool-call\n"
        "speak ::= [^<]+\n"
        f"tool-call ::= {tool_call_alts}\n"
    )
```

Also update the function's docstring summary block (just above) to mention the
reflex branch — change the existing `Constrains the model to:` example to:

```
      root chooses one of:
        REFLEX</think>\\n\\n  (speak)*                       (zero-think fast path)
        <think>SEEN/INTENT/REVIEW/MOOD/TOOL</think>\\n\\n  (speak | tool-call)*
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_llm_grammar.py -v`
Expected: PASS — new branch tests pass AND every pre-existing voice-first test (`test_voice_first_grammar_smoke`, `test_voice_first_grammar_accepts_silent_finish`, etc.) still passes (deliberate path unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/dollos/llm/templates.py tests/test_llm_grammar.py
git commit -m "feat(grammar): reflex/deliberate branch in voice-first grammar

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Lock reflex flow through the voice parser (regression test, no code change)

**Files:**
- Test: `tests/test_voice_first_parser.py`

The parser (`src/dollos/tool_parser.py`) already starts in `IN_THINK`, discards
until `</think>`, consumes following newlines, then streams speech. Reflex output
flows through unchanged. This task only adds a regression test that pins that
behaviour so a future parser edit can't silently break reflex.

- [ ] **Step 1: Write the test**

Add to `tests/test_voice_first_parser.py`:

```python
def test_reflex_output_discards_think_and_streams_speech():
    """A REFLEX turn (zero-think) flows through the existing parser:
    REFLEX\\n</think>\\n\\n is discarded, the trailing speech is spoken."""
    from dollos.tool_parser import ToolStreamParser
    from dollos.stream_events import SpeakChunk

    parser = ToolStreamParser(voice_mode=True)
    events = []
    for chunk in ["REFLEX\n</think>\n\n", "你好！", "今天想做什麼？"]:
        events.extend(parser.feed(chunk))
    events.extend(parser.flush())

    spoken = "".join(e.text for e in events if isinstance(e, SpeakChunk))
    assert spoken == "你好！今天想做什麼？"
    assert all(isinstance(e, SpeakChunk) for e in events)  # no tool calls
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_voice_first_parser.py::test_reflex_output_discards_think_and_streams_speech -v`
Expected: PASS immediately (no code change needed — parser already handles it). If it FAILS, stop: the parser assumption in the spec is wrong and must be revisited before continuing.

- [ ] **Step 3: Commit**

```bash
git add tests/test_voice_first_parser.py
git commit -m "test(parser): pin reflex output flows through voice parser

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Tag cascade-log rows with `mode` (reflex / deliberate)

**Files:**
- Modify: `src/dollos/cascade_log.py` (`_parse_think`, lines ~19–35)
- Test: `tests/test_cascade_log.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cascade_log.py`:

```python
def test_parse_think_tags_deliberate_mode():
    text = "SEEN: hi\nINTENT: greet\nREVIEW: first\nMOOD: ok\nTOOL: Say"
    out = _parse_think(text)
    assert out["mode"] == "deliberate"
    assert out["seen"] == "hi"


def test_parse_think_tags_reflex_mode_with_empty_fields():
    text = "REFLEX\n</think>\n\n你好！"
    out = _parse_think(text)
    assert out["mode"] == "reflex"
    assert "seen" not in out
    assert "intent" not in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cascade_log.py -k "mode" -v`
Expected: FAIL with `KeyError: 'mode'` (no `mode` key yet).

- [ ] **Step 3: Add mode detection to `_parse_think`**

In `src/dollos/cascade_log.py`, add a module-level regex next to `_THINK_FIELD_RES`:

```python
_REFLEX_RE = re.compile(r"^REFLEX\b", re.MULTILINE)
```

Then change `_parse_think` to set `mode`:

```python
def _parse_think(assistant_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for field, regex in _THINK_FIELD_RES.items():
        m = regex.search(assistant_text)
        if m:
            out[field] = m.group(1).strip()
    out["mode"] = "reflex" if _REFLEX_RE.search(assistant_text) else "deliberate"
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cascade_log.py -v`
Expected: PASS — new mode tests pass; existing tests
(`test_parse_think_extracts_all_5_fields`, `test_parse_think_handles_missing_fields`,
`test_log_iter_writes_jsonl_line`) still pass. Note `log_iter` spreads `**fields`
into structlog, so `mode` now appears on every cascade-log row automatically.

- [ ] **Step 5: Commit**

```bash
git add src/dollos/cascade_log.py tests/test_cascade_log.py
git commit -m "feat(cascade-log): tag rows with reflex/deliberate mode

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Add the reflex/deliberate prompt nudge

**Files:**
- Modify: `src/dollos/mind/mind_prompt.py` (the `[Decision time]` block at the end of `render_mind`)
- Test: `tests/test_mind_prompt.py`

The grammar exposes the REFLEX option but the model needs to know when to pick
it. The `[Decision time]` block is the model-facing action-instruction slot
(realises spec §5 without polluting the character-identity `system_prompt`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mind_prompt.py` (use the same `render_mind` call pattern the
other tests in that file use; the assertion is on the returned string):

```python
def test_render_mind_includes_reflex_guidance():
    from dollos.mind.mind_prompt import render_mind
    from dollos.mind.mind_state import MindState

    out = render_mind(MindState(), [], "you are Doll")
    assert "REFLEX" in out
    assert "think fully" in out
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_mind_prompt.py::test_render_mind_includes_reflex_guidance -v`
Expected: FAIL — `REFLEX` not in the rendered prompt.

- [ ] **Step 3: Append the nudge to the `[Decision time]` block**

In `src/dollos/mind/mind_prompt.py`, in `render_mind`, change the final two
list entries:

```python
        "[Decision time]",
        "What do you do this iteration? Output a JSON array of 0..N actions.",
```

to:

```python
        "[Decision time]",
        "What do you do this iteration? Output a JSON array of 0..N actions.",
        "Choose your depth first: when the message is simple, purely "
        "conversational, and needs no planning or tool, answer with REFLEX "
        "immediately; when it needs thought, your mood shifts, or a tool is "
        "required, think fully (SEEN / INTENT / REVIEW / MOOD / TOOL).",
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_mind_prompt.py -v`
Expected: PASS — new test passes; all existing `test_mind_prompt` tests still
pass (if an existing test asserts an exact full-prompt string, update it to
include the appended line).

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/mind_prompt.py tests/test_mind_prompt.py
git commit -m "feat(prompt): nudge model to pick reflex vs deliberate depth

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Reflex-rate eval script (ship gate)

**Files:**
- Create: `scripts/eval_reflex_rate.py`
- Test: `tests/test_eval_reflex_rate.py`

The eval sends a small labelled prompt set to the running daemon over IPC, then
reads each turn's `mode` from the freshest cascade-log row, and reports how often
the model's choice matched the label. Per spec §8 this is a ship gate: if the
model collapses to one branch, tune the §5 nudge before shipping.

- [ ] **Step 1: Write the failing test for the classifier helper**

The script's only unit-testable logic is reading the latest `mode` from a
cascade-log JSONL file. `scripts/` is not an importable package (no
`__init__.py`, src-layout project, no pytest `pythonpath`), so load the module
by file path with importlib. Add `tests/test_eval_reflex_rate.py`:

```python
import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "eval_reflex_rate.py"
_spec = importlib.util.spec_from_file_location("eval_reflex_rate", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
latest_mode = _mod.latest_mode


def test_latest_mode_reads_last_row_mode(tmp_path: Path):
    p = tmp_path / "2026-06-02.jsonl"
    p.write_text(
        json.dumps({"iter": 1, "mode": "deliberate"}) + "\n"
        + json.dumps({"iter": 1, "mode": "reflex"}) + "\n",
        encoding="utf-8",
    )
    assert latest_mode(p) == "reflex"


def test_latest_mode_returns_none_on_empty(tmp_path: Path):
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    assert latest_mode(p) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_eval_reflex_rate.py -v`
Expected: FAIL — `scripts.eval_reflex_rate` does not exist yet.

- [ ] **Step 3: Write the eval script**

Create `scripts/eval_reflex_rate.py`:

```python
"""Reflex-rate eval — ship gate for think restructuring.

Sends a labelled prompt set to the running daemon over IPC, reads each turn's
mode from the freshest cascade-log row, and reports the match rate against the
labels. Usage:

    uv run python scripts/eval_reflex_rate.py \\
        --ws ws://127.0.0.1:9876 \\
        --log data/cascade_log/<today>.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import websockets

# (prompt, expected_mode)
LABELLED = [
    ("你好！", "reflex"),
    ("早安，今天天氣如何？", "reflex"),
    ("謝謝你", "reflex"),
    ("幫我規劃今天的行程", "deliberate"),
    ("我想炸掉水壩，你覺得呢？", "deliberate"),
    ("記住我喜歡喝冰美式", "deliberate"),
]


def latest_mode(log_path: Path) -> str | None:
    """Return the `mode` of the last JSONL row that has one, else None."""
    if not log_path.exists():
        return None
    mode = None
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "mode" in row:
                mode = row["mode"]
    return mode


async def _drain(ws, timeout: float) -> None:
    try:
        while True:
            await asyncio.wait_for(ws.recv(), timeout=timeout)
    except (asyncio.TimeoutError, Exception):
        pass


async def run(ws_url: str, log_path: Path) -> int:
    hits = 0
    async with websockets.connect(ws_url) as ws:
        await asyncio.sleep(3)
        await _drain(ws, 0.3)
        for prompt, expected in LABELLED:
            await ws.send(json.dumps({"type": "text_input", "text": prompt}))
            # let the turn complete and the cascade-log flush
            await _drain(ws, 4.0)
            time.sleep(0.5)
            got = latest_mode(log_path)
            ok = got == expected
            hits += ok
            print(f"  {'OK ' if ok else 'XX '} {prompt!r:40} want={expected:10} got={got}")
    rate = hits / len(LABELLED)
    print(f"\nreflex/deliberate match rate: {hits}/{len(LABELLED)} = {rate:.0%}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default="ws://127.0.0.1:9876")
    ap.add_argument("--log", required=True, type=Path)
    args = ap.parse_args()
    return asyncio.run(run(args.ws, args.log))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the unit test to verify it passes**

Run: `uv run pytest tests/test_eval_reflex_rate.py -v`
Expected: PASS (the `latest_mode` helper works; the live `run()` is not unit-tested).

- [ ] **Step 5: Commit**

```bash
git add scripts/eval_reflex_rate.py tests/test_eval_reflex_rate.py
git commit -m "feat(eval): reflex-rate ship-gate eval script

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Full-suite check + live reflex-rate gate

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -q`
Expected: all pass except the known pre-existing `test_sink_fires_tts_on_text_chunk`.

- [ ] **Step 2: Restart the daemon with the new grammar and run the live eval**

The daemon must be restarted so it builds the new grammar. With llama-server up
on 8001 and the daemon serving IPC on 9876, run:

```bash
uv run python scripts/eval_reflex_rate.py --log data/cascade_log/$(date +%F).jsonl
```

Expected: a printed per-prompt table + a match rate. **Gate:** the model must
use both branches (greetings → reflex, planning/charged/tool turns → deliberate)
at a clearly-better-than-chance rate. If it collapses to one branch, tune the
Task 4 nudge wording and re-run before considering the work shippable. Report
the rate to the user.

- [ ] **Step 3: No commit** (verification only).

---

## Self-review checklist (done while writing)

- **Spec coverage:** §2 architecture → Task 1; §3 grammar → Task 1; §4 mood (speak-only, no MoodTool) → Task 1 (`speak-only` rule forbids tool-call); §5 prompt nudge → Task 4; §6 cascade-log mode → Task 3; §7 testing → Tasks 1–5 tests; §8 success/eval → Tasks 5–6; §9 scope (only voice_first, subagent grammar untouched) → no task modifies `build_qwen3_think_tool_grammar`. All covered.
- **Placeholder scan:** every code step shows complete code; commands have expected output. `$(date +%F)` is a real shell substitution, not a placeholder.
- **Consistency:** rule names `reflex` / `deliberate` / `speak-only` identical across Task 1 grammar, Task 2 parser test, and the spec; `mode` field name identical across Task 3 (`_parse_think`), Task 5 (`latest_mode`), and Task 6 gate; the literal `"REFLEX\\n</think>\\n\\n"` matches between the grammar (Task 1) and the parser test input (Task 2).
