# Tool Memory & Habits (Spec B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 跨-turn 工具學習迴路——Layer 1 機械型失敗記憶（live dispatch 記錄 + 精簡 [Tool notes]）+ Layer 2 ACE-style append-only playbook（idle reflector distill 工具課程，[Tool habits] surface）。減少重蹈型 fail-tooling + 形成慣性。

**Architecture:** 記錄掛在 `mind_loop._dispatch_tool`（live-only，dispatch_one 之後）。MindState 持有 `tool_stats` + `recent_tool_failures`。新模組 `mind/tool_memory.py` 集中邏輯。Playbook 是 `NoteToolLesson` 工具（reflection-only registry）寫進 `tool_playbook.md`、FtsMemory 索引，經 `source_prefix` 檢索 surface。

**Tech Stack:** Python 3、pydantic v2、dataclasses + deque、FtsMemory（SQLite FTS5，`search(query, top_k, source_prefix)`）、GBNF、pytest（asyncio）。

## Global Constraints

- **記錄是觀測性，非 no-fallback 範疇**：`record_tool_outcome` 包 try/except，記錄失敗絕不讓 dispatch 失敗或 turn 崩。
- **Playbook append-only**：`NoteToolLesson` 永不改寫既有 entry（避免 context collapse）。
- **記錄只在 live wrapper（`_dispatch_tool`）**：絕不搬進 `dispatch_one`（會污染 subagent）。subagent 隔離負向測試守此界。
- **`NoteToolLesson` reflection-only**：不進 `MAIN_TOOLS`；只在 reflection turn 進 registry/grammar；safe_mode 優先於 reflection。
- **timestamp-only playbook heading**：不用 `build_heading`（避免軸標籤洩漏進 [Associative memories]）。
- **hot-path gating**：`[Tool notes]` 無近期失敗不 render；`tool_habits_search` 無 tool_stats / 無 playbook 不查。
- **不破壞 Spec A**：全套既有 645 tests 維持綠。
- TDD；測試指令 `cd /home/progcat/Projects/DollOS && uv run pytest <path> -q`；branch `feat/tool-memory`。

---

## File Structure

- `src/dollos/mind/mind_state.py` — `ToolFailure` dataclass + `tool_stats`/`recent_tool_failures` 欄位 + save/load。
- `src/dollos/mind/tool_memory.py`（新）— record + render + search 全部邏輯。
- `src/dollos/mind/mind_loop.py` — `_dispatch_tool` 記錄；`iterate` 算 is_reflection + 兩個 side-channel block；reflection registry/grammar。
- `src/dollos/mind/mind_prompt.py` — `render_mind` 三個新 block 參數 + `_percep_body` nudge。
- `src/dollos/tools.py` — `NoteToolLesson` + `REFLECTION_TOOLS`。
- `tests/` — test_tool_memory.py（新）、test_mind_state.py、test_mind_loop.py、test_tools.py、test_mind_prompt.py、test_tool_loop.py。

---

## Task 1: MindState — ToolFailure + 欄位 + save/load round-trip

**Files:** Modify `src/dollos/mind/mind_state.py`; Test `tests/test_mind_state.py`

**Interfaces — Produces:**
- `ToolFailure(t: float, tool: str, detail: str)` dataclass
- `MindState.tool_stats: dict[str, dict[str, int]]`、`MindState.recent_tool_failures: deque[ToolFailure]`（maxlen 10）
- save/load 保留兩者

- [ ] **Step 1: 失敗測試** — 加到 `tests/test_mind_state.py`：
```python
def test_tool_memory_fields_roundtrip(tmp_path):
    from collections import deque
    from dollos.mind.mind_state import MindState, ToolFailure, save_state, load_state
    s = MindState()
    s.tool_stats = {"Shell": {"ok": 3, "fail": 1}}
    s.recent_tool_failures = deque(
        [ToolFailure(t=123.0, tool="Shell", detail="timeout")], maxlen=10
    )
    p = tmp_path / "s.json"
    assert save_state(s, p)
    loaded = load_state(p)
    assert loaded.tool_stats == {"Shell": {"ok": 3, "fail": 1}}
    assert len(loaded.recent_tool_failures) == 1
    assert loaded.recent_tool_failures[0].tool == "Shell"
    assert loaded.recent_tool_failures[0].detail == "timeout"
    assert loaded.recent_tool_failures.maxlen == 10
```
- [ ] **Step 2: 跑確認失敗** — `uv run pytest tests/test_mind_state.py::test_tool_memory_fields_roundtrip -q` → FAIL（欄位不存在）。
- [ ] **Step 3: 實作**
  - 在 `OutputRecord` dataclass 之後新增：
```python
@dataclass
class ToolFailure:
    t: float
    tool: str
    detail: str
```
  - `MindState` 內（在 `recent_reviews` 附近）新增欄位：
```python
    tool_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    recent_tool_failures: deque[ToolFailure] = field(
        default_factory=lambda: deque(maxlen=10)
    )
```
  - `save_state` 內，在處理 `recent_reviews` 那行之後新增：
```python
        state_dict["recent_tool_failures"] = [
            asdict(f) for f in state.recent_tool_failures
        ]
        state_dict["tool_stats"] = dict(state.tool_stats)
```
  - `load_state` 內，在 `recent_reviews = ...` 之後新增重建，並把兩者傳進 `MindState(...)`：
```python
        recent_tool_failures = deque(
            [_coerce(ToolFailure, f) for f in data.get("recent_tool_failures", [])],
            maxlen=10,
        )
        tool_stats = data.get("tool_stats", {})
```
    並在 `MindState(...)` 建構參數加入 `recent_tool_failures=recent_tool_failures, tool_stats=tool_stats,`。
- [ ] **Step 4: 跑通過** — `uv run pytest tests/test_mind_state.py -q` → PASS（含既有）。
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(mind-state): ToolFailure + tool_stats/recent_tool_failures fields with save/load"`

---

## Task 2: tool_memory.py — record_tool_outcome + render_tool_notes

**Files:** Create `src/dollos/mind/tool_memory.py`; Test `tests/test_tool_memory.py`（新）

**Interfaces — Consumes:** `MindState`, `ToolFailure`（Task 1）, `ToolResult`（`dollos.cascade.tool_loop`）.
**Produces:** `record_tool_outcome(state, name, result)`、`render_tool_notes(recent_tool_failures, now) -> str | None`、常數 `TOOL_NOTE_WINDOW_S=3600.0`。

- [ ] **Step 1: 失敗測試** — 新檔 `tests/test_tool_memory.py`：
```python
"""Tests for tool_memory (Spec B)."""
from __future__ import annotations
from collections import deque
from dollos.cascade.tool_loop import ToolResult
from dollos.mind.mind_state import MindState, ToolFailure
from dollos.mind.tool_memory import record_tool_outcome, render_tool_notes


def test_record_success_none_and_fail():
    s = MindState()
    record_tool_outcome(s, "Recall", ToolResult("Recall", True, "hit"))
    record_tool_outcome(s, "Shell", None)  # side-effect ok
    record_tool_outcome(s, "ReadToolOutput", ToolResult("ReadToolOutput", False, "limit 需 1–500"))
    assert s.tool_stats["Recall"] == {"ok": 1, "fail": 0}
    assert s.tool_stats["Shell"] == {"ok": 1, "fail": 0}
    assert s.tool_stats["ReadToolOutput"] == {"ok": 0, "fail": 1}
    assert len(s.recent_tool_failures) == 1
    assert s.recent_tool_failures[0].tool == "ReadToolOutput"


def test_record_never_raises_on_bad_result():
    s = MindState()
    record_tool_outcome(s, "X", object())  # no .success attr → swallowed
    # no exception; nothing recorded for the bad path is fine


def test_render_tool_notes_gated_aged_deduped():
    now = 1000.0
    fails = deque([
        ToolFailure(t=now - 10, tool="Shell", detail="timeout A"),
        ToolFailure(t=now - 5, tool="Shell", detail="timeout B"),   # newer Shell → wins dedup
        ToolFailure(t=now - 99999, tool="OldTool", detail="ancient"),  # aged out
    ], maxlen=10)
    out = render_tool_notes(fails, now)
    assert out is not None
    assert "timeout B" in out and "timeout A" not in out  # dedup keeps latest
    assert "OldTool" not in out  # aged out (>1h)
    assert render_tool_notes(deque(maxlen=10), now) is None  # no failures → no block
```
- [ ] **Step 2: 跑確認失敗** — `uv run pytest tests/test_tool_memory.py -q` → FAIL（ImportError）。
- [ ] **Step 3: 實作** — 新檔 `src/dollos/mind/tool_memory.py`：
```python
"""Tool memory — per-tool outcome stats, recent-failure notes, and the
append-only tool-lesson playbook surfacing (Spec B)."""
from __future__ import annotations

import logging
import time
from collections import deque

from dollos.mind.mind_state import MindState, ToolFailure

logger = logging.getLogger(__name__)

TOOL_NOTE_WINDOW_S = 3600.0  # only surface failures from the last hour
_MAX_TOOL_NOTES = 5
_DETAIL_CAP = 100


def record_tool_outcome(state: MindState, name: str, result) -> None:
    """Record one tool dispatch outcome. Observability only — never raises.

    result: None (side-effect tool ran cleanly) or a ToolResult.
    """
    try:
        stat = state.tool_stats.setdefault(name, {"ok": 0, "fail": 0})
        if result is None or result.success:
            stat["ok"] += 1
        else:
            stat["fail"] += 1
            state.recent_tool_failures.append(
                ToolFailure(t=time.time(), tool=name, detail=(result.detail or "")[:200])
            )
    except Exception:
        logger.exception("record_tool_outcome failed for %s; continuing", name)


def render_tool_notes(recent_tool_failures: deque, now: float) -> str | None:
    """[Tool notes] block from recent failures, or None when none are recent.

    Window-filtered, deduped by tool (latest wins), newest-first, capped.
    """
    recent = [f for f in recent_tool_failures if now - f.t < TOOL_NOTE_WINDOW_S]
    if not recent:
        return None
    latest: dict[str, ToolFailure] = {}
    for f in recent:
        if f.tool not in latest or f.t > latest[f.tool].t:
            latest[f.tool] = f
    ordered = sorted(latest.values(), key=lambda f: f.t, reverse=True)[:_MAX_TOOL_NOTES]
    lines = [f"- {f.tool}: {f.detail[:_DETAIL_CAP]}" for f in ordered]
    return "[Tool notes] 最近工具失敗（避免重蹈同樣錯誤）：\n" + "\n".join(lines)
```
- [ ] **Step 4: 跑通過** — `uv run pytest tests/test_tool_memory.py -q` → PASS。
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(tool-memory): record_tool_outcome + render_tool_notes (Layer 1 core)"`

---

## Task 3: 接線 Layer 1 — _dispatch_tool 記錄 + [Tool notes] + 隔離測試 + dispatch_one runtime 測試

**Files:** Modify `src/dollos/mind/mind_loop.py`, `src/dollos/mind/mind_prompt.py`; Test `tests/test_mind_loop.py`, `tests/test_mind_prompt.py`, `tests/test_tool_loop.py`

**Interfaces — Consumes:** `record_tool_outcome`, `render_tool_notes`（Task 2）.

- [ ] **Step 1: 失敗測試**
  - `tests/test_tool_loop.py`（補 Spec A 遞延的 runtime-error 分支）：
```python
@pytest.mark.asyncio
async def test_dispatch_one_runtime_error_returns_failed_result(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.cascade.tool_loop import dispatch_one
    from pydantic import BaseModel

    class _Boom(BaseModel):
        async def run(self, ctx):
            raise RuntimeError("kaboom")

    ctx = _make_mind_ctx(tmp_path)
    r = await dispatch_one("Boom", {}, ctx, {"Boom": _Boom})
    assert r is not None and r.success is False
    assert "runtime error" in r.detail and "kaboom" in r.detail
```
  - `tests/test_mind_loop.py`（記錄 + 隔離）：
```python
@pytest.mark.asyncio
async def test_dispatch_tool_records_outcome(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.mind.mind_state import MindState
    from dollos.mind.perception_queue import PerceptionQueue
    state = MindState()
    ctx = _make_mind_ctx(tmp_path, state=state)
    loop = MindLoop(state=state, queue=PerceptionQueue(), ctx=ctx, llm=_FakeLLM(""),
                    system_prompt="", state_persist_path=tmp_path / "s.json",
                    tool_registry={cls.__name__: cls for cls in __import__(
                        "dollos.tools", fromlist=["MAIN_TOOLS"]).MAIN_TOOLS})
    await loop._dispatch_tool("SetFocus", {"text": "x"})        # success
    await loop._dispatch_tool("NoSuchTool", {})                 # unknown → fail
    assert state.tool_stats["SetFocus"]["ok"] == 1
    assert state.tool_stats["NoSuchTool"]["fail"] == 1
    assert any(f.tool == "NoSuchTool" for f in state.recent_tool_failures)


@pytest.mark.asyncio
async def test_subagent_dispatch_does_not_record_to_doll(tmp_path):
    """Isolation: the subagent path (dispatch_tool_call) must NOT touch Doll's
    tool memory — recording lives only in the live wrapper."""
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.cascade.tool_loop import dispatch_tool_call
    ctx = _make_mind_ctx(tmp_path)
    await dispatch_tool_call({"name": "SetFocus", "arguments": {"text": "x"}}, ctx,
                             {"SetFocus": __import__("dollos.tools", fromlist=["SetFocus"]).SetFocus})
    assert ctx.mind_state.tool_stats == {}
    assert len(ctx.mind_state.recent_tool_failures) == 0
```
  - `tests/test_mind_prompt.py`（[Tool notes] block）：
```python
def test_render_mind_includes_tool_notes_when_recent_failures():
    import time
    from collections import deque
    from dollos.mind.mind_state import MindState, ToolFailure
    from dollos.mind.mind_prompt import render_mind
    s = MindState()
    s.recent_tool_failures = deque(
        [ToolFailure(t=time.time(), tool="Shell", detail="timeout after 60s")], maxlen=10
    )
    out = render_mind(s, [], "sys")
    assert "[Tool notes]" in out and "Shell" in out and "timeout" in out


def test_render_mind_omits_tool_notes_when_no_failures():
    from dollos.mind.mind_state import MindState
    from dollos.mind.mind_prompt import render_mind
    out = render_mind(MindState(), [], "sys")
    assert "[Tool notes]" not in out
```
- [ ] **Step 2: 跑確認失敗** — `uv run pytest tests/test_tool_loop.py tests/test_mind_loop.py tests/test_mind_prompt.py -q` → 新測試 FAIL。
- [ ] **Step 3: 實作**
  - `mind_loop.py` import：`from dollos.mind.tool_memory import record_tool_outcome`。
  - `_dispatch_tool` 改為：
```python
    async def _dispatch_tool(self, name: str, arguments: dict) -> ToolResult | None:
        """Dispatch via shared dispatch_one (spec §3.6), then record the outcome
        into Doll's tool memory (Spec B Layer 1 — live-only)."""
        r = await dispatch_one(name, arguments, self._ctx, self._active_tool_registry())
        record_tool_outcome(self._ctx.mind_state, name, r)
        return r
```
  - `mind_prompt.py`：import `from dollos.mind.tool_memory import render_tool_notes`。在 `render_mind` 內，於 `[Recent perceptions]` block 之前插入（gated）：
```python
    tool_notes = render_tool_notes(state.recent_tool_failures, now)
    if tool_notes:
        blocks.extend([tool_notes, ""])
```
- [ ] **Step 4: 跑通過** — `uv run pytest tests/test_tool_loop.py tests/test_mind_loop.py tests/test_mind_prompt.py -q` → PASS。
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(mind): record tool outcomes in live _dispatch_tool + [Tool notes] block; dispatch_one runtime test"`

---

## Task 4: NoteToolLesson 工具 + REFLECTION_TOOLS

**Files:** Modify `src/dollos/tools.py`; Test `tests/test_tools.py`

**Interfaces — Produces:** `NoteToolLesson(situation, lesson)`、`REFLECTION_TOOLS`。

- [ ] **Step 1: 失敗測試** — `tests/test_tools.py`：
```python
@pytest.mark.asyncio
async def test_note_tool_lesson_appends_and_indexes(tmp_path):
    from dollos.tools import NoteToolLesson
    ctx, ms, _sink = _make_ctx(tmp_path)
    out = await NoteToolLesson(situation="grepping large output",
                               lesson="use GrepToolOutput, not Shell grep").run(ctx)
    assert out.startswith("lesson noted:")
    path = tmp_path / "shared" / "tool_playbook.md"
    text = path.read_text()
    assert "[situation] grepping large output" in text
    assert "use GrepToolOutput" in text
    # timestamp-only heading (no [k:v] axis tags that would leak into associative)
    import re
    heading = next(l for l in text.splitlines() if l.startswith("## "))
    assert re.match(r"## \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", heading)
    # append-only: a second lesson adds, never overwrites
    await NoteToolLesson(situation="s2", lesson="l2").run(ctx)
    assert "[situation] grepping large output" in path.read_text()
    assert "[situation] s2" in path.read_text()


def test_note_tool_lesson_not_in_main_tools_but_in_reflection_tools():
    from dollos.tools import MAIN_TOOLS, REFLECTION_TOOLS, NoteToolLesson
    assert NoteToolLesson not in MAIN_TOOLS
    assert NoteToolLesson in REFLECTION_TOOLS
    assert all(t in REFLECTION_TOOLS for t in MAIN_TOOLS)
```
- [ ] **Step 2: 跑確認失敗** — `uv run pytest tests/test_tools.py -k note_tool_lesson -q` → FAIL。
- [ ] **Step 3: 實作** — `tools.py`（`MoodTool` 之後、`MAIN_TOOLS` 之前）：
```python
class NoteToolLesson(BaseModel):
    """Record a compact, reusable lesson about HOW to use your tools —
    distilled from what worked or what failed. Append-only: write a NEW
    lesson rather than rewriting an old one. (Surfaced back as [Tool habits].)"""

    situation: str = Field(description="When this applies, one short phrase.")
    lesson: str = Field(description="The reusable takeaway, one or two sentences.")

    def _summary(self) -> str:
        return f"tool lesson: {self.situation[:60]}"

    async def run(self, ctx: "MindCtx") -> str:
        path = ctx.memory_root / "shared" / "tool_playbook.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        heading = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        with path.open("a") as f:
            f.write(f"\n## {heading}\n\n[situation] {self.situation}\n{self.lesson}\n")
        await ctx.memsearch.index_file(path)
        _record(ctx, "NoteToolLesson", self._summary())
        return f"lesson noted: {self.situation[:60]}"
```
  （`datetime` 已在 `tools.py` import。）在 `MAIN_TOOLS` 定義之後新增：
```python
REFLECTION_TOOLS: list[type[BaseModel]] = MAIN_TOOLS + [NoteToolLesson]
```
- [ ] **Step 4: 跑通過** — `uv run pytest tests/test_tools.py -q` → PASS。
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(tools): NoteToolLesson (append-only playbook) + REFLECTION_TOOLS registry"`

---

## Task 5: reflection-only registry/grammar 接線

**Files:** Modify `src/dollos/mind/mind_loop.py`; Test `tests/test_mind_loop.py`

**Interfaces — Consumes:** `REFLECTION_TOOLS`, `NoteToolLesson`（Task 4）.
**Produces:** `MindLoop._is_reflection` flag；`_active_tool_registry()`/`_active_grammar()` reflection-aware（safe_mode 優先）。

- [ ] **Step 1: 失敗測試** — `tests/test_mind_loop.py`：
```python
def test_active_registry_reflection_includes_note_tool_lesson(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.mind.mind_state import MindState
    from dollos.mind.perception_queue import PerceptionQueue
    from dollos.tools import MAIN_TOOLS
    state = MindState()
    loop = MindLoop(state=state, queue=PerceptionQueue(), ctx=_make_mind_ctx(tmp_path, state=state),
                    llm=_FakeLLM(""), system_prompt="", state_persist_path=tmp_path / "s.json",
                    tool_registry={c.__name__: c for c in MAIN_TOOLS})
    loop._is_reflection = False
    assert "NoteToolLesson" not in loop._active_tool_registry()
    loop._is_reflection = True
    assert "NoteToolLesson" in loop._active_tool_registry()
    # safe_mode wins over reflection
    state.safe_mode = True
    assert "NoteToolLesson" not in loop._active_tool_registry()
```
- [ ] **Step 2: 跑確認失敗** — `uv run pytest tests/test_mind_loop.py -k reflection_includes -q` → FAIL。
- [ ] **Step 3: 實作** — `mind_loop.py`：
  - import：`from dollos.tools import NoteToolLesson`。
  - `__init__` 末尾新增：`self._is_reflection = False`、`self._reflection_grammar: str | None = None`。
  - `_active_tool_registry()` 改為（safe_mode 優先 → reflection → base）：
```python
    def _active_tool_registry(self) -> dict[str, type[BaseModel]]:
        if self._state.safe_mode:
            return {n: c for n, c in self._tool_registry.items() if n in SAFE_MODE_TOOLS}
        if self._is_reflection:
            return {**self._tool_registry, "NoteToolLesson": NoteToolLesson}
        return self._tool_registry
```
  - `_active_grammar()` 改為（在現有 safe_mode 分支後、return self._grammar 前插入 reflection 分支）：
```python
        if self._state.safe_mode:
            if self._safe_grammar is None:
                self._safe_grammar = build_voice_first_grammar(
                    list(self._active_tool_registry().values())
                )
            return self._safe_grammar
        if self._is_reflection:
            if self._reflection_grammar is None:
                self._reflection_grammar = build_voice_first_grammar(
                    list(self._active_tool_registry().values())
                )
            return self._reflection_grammar
        return self._grammar
```
  （注意：原 `_active_grammar` 的 safe_mode 段落須保留語意；上面整合呈現——實作時按既有結構插入 reflection 分支即可。）
  - `iterate()`：在 drain + recent_perceptions append 之後、render 之前，加：
```python
        self._is_reflection = any(p.kind == "ReflectionMoment" for p in perceptions)
```
- [ ] **Step 4: 跑通過** — `uv run pytest tests/test_mind_loop.py -q` → PASS（含既有 grammar-cliff 等測試）。
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(mind): reflection-only registry/grammar (NoteToolLesson gated to reflection turns; safe_mode priority)"`

---

## Task 6: [Tool outcomes] block（reflection-only）+ ReflectionMoment nudge

**Files:** Modify `src/dollos/mind/tool_memory.py`, `src/dollos/mind/mind_loop.py`, `src/dollos/mind/mind_prompt.py`; Test `tests/test_tool_memory.py`, `tests/test_mind_prompt.py`

**Interfaces — Produces:** `render_tool_outcomes(tool_stats, recent_tool_failures) -> str`；`render_mind(..., tool_outcomes_block: str | None = None)`。

- [ ] **Step 1: 失敗測試**
  - `tests/test_tool_memory.py`：
```python
def test_render_tool_outcomes_has_counts_and_failure_snippet():
    from collections import deque
    from dollos.mind.mind_state import ToolFailure
    from dollos.mind.tool_memory import render_tool_outcomes
    stats = {"Shell": {"ok": 3, "fail": 1}, "Recall": {"ok": 5, "fail": 0}}
    fails = deque([ToolFailure(t=1.0, tool="Shell", detail="timeout after 60s")], maxlen=10)
    out = render_tool_outcomes(stats, fails)
    assert "Shell" in out and "3 ok" in out and "1 fail" in out
    assert "timeout after 60s" in out
    assert "Recall" in out and "5 ok" in out
```
  - `tests/test_mind_prompt.py`：
```python
def test_tool_outcomes_block_only_when_passed():
    from dollos.mind.mind_state import MindState
    from dollos.mind.mind_prompt import render_mind
    s = MindState()
    assert "[Tool outcomes" not in render_mind(s, [], "sys")
    out = render_mind(s, [], "sys", tool_outcomes_block="[Tool outcomes since last reflection]\n- Shell: 3 ok, 1 fail")
    assert "[Tool outcomes" in out and "Shell" in out
```
- [ ] **Step 2: 跑確認失敗** → FAIL。
- [ ] **Step 3: 實作**
  - `tool_memory.py` 新增：
```python
_MAX_OUTCOME_FAILS = 3
_OUTCOME_DETAIL_CAP = 100


def render_tool_outcomes(tool_stats: dict, recent_tool_failures: deque) -> str:
    """[Tool outcomes since last reflection] — per-tool ok/fail + recent fail samples.
    Reflection-only; caller gates on is_reflection."""
    lines = ["[Tool outcomes since last reflection]"]
    last_fail: dict[str, str] = {}
    for f in list(recent_tool_failures)[-_MAX_OUTCOME_FAILS:]:
        last_fail[f.tool] = f.detail[:_OUTCOME_DETAIL_CAP]
    for tool, st in tool_stats.items():
        line = f"- {tool}: {st.get('ok', 0)} ok, {st.get('fail', 0)} fail"
        if tool in last_fail:
            line += f" — last fail: {last_fail[tool]}"
        lines.append(line)
    return "\n".join(lines[:20])
```
  - `mind_prompt.py` `render_mind` 新增參數 `tool_outcomes_block: str | None = None`，並在 `[Tool notes]` 附近（reflection context 區）插入：
```python
    if tool_outcomes_block:
        blocks.extend([tool_outcomes_block, ""])
```
  - `_percep_body` 的 `ReflectionMoment` 分支文案，在既有句尾追加：`「若有可重用的工具用法或陷阱，用 NoteToolLesson 記下來」`。
  - `mind_loop.iterate`：在算出 `self._is_reflection` 後、render 前：
```python
        tool_outcomes_block = None
        if self._is_reflection:
            from dollos.mind.tool_memory import render_tool_outcomes
            tool_outcomes_block = render_tool_outcomes(
                self._state.tool_stats, self._state.recent_tool_failures
            )
```
    並把 `tool_outcomes_block=tool_outcomes_block` 傳進 `render_mind(...)`。
- [ ] **Step 4: 跑通過** — `uv run pytest tests/test_tool_memory.py tests/test_mind_prompt.py tests/test_mind_loop.py -q` → PASS。
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(mind): [Tool outcomes] reflection block + NoteToolLesson nudge"`

---

## Task 7: tool_habits_search + [Tool habits] surface

**Files:** Modify `src/dollos/mind/tool_memory.py`, `src/dollos/mind/mind_loop.py`, `src/dollos/mind/mind_prompt.py`; Test `tests/test_tool_memory.py`, `tests/test_mind_prompt.py`

**Interfaces — Produces:** `tool_habits_search(memsearch, state, playbook_path, top_k=2) -> list[dict]`、`_parse_playbook_chunk(content) -> tuple[str,str] | None`、`render_tool_habits(hits) -> str`；`render_mind(..., tool_habits_hits: list[dict] | None = None)`。

- [ ] **Step 1: 失敗測試** — `tests/test_tool_memory.py`：
```python
import pytest


def test_parse_playbook_chunk():
    from dollos.mind.tool_memory import _parse_playbook_chunk
    chunk = "## 2026-06-27 10:00:00\n\n[situation] grepping output\nuse GrepToolOutput\n"
    assert _parse_playbook_chunk(chunk) == ("grepping output", "use GrepToolOutput")
    assert _parse_playbook_chunk("garbage with no situation") is None


@pytest.mark.asyncio
async def test_tool_habits_search_gated_and_source_restricted(tmp_path):
    from dollos.mind.mind_state import MindState
    from dollos.mind.tool_memory import tool_habits_search

    class _FakeMem:
        def __init__(self): self.calls = []
        async def search(self, q, top_k=5, source_prefix=None):
            self.calls.append({"q": q, "top_k": top_k, "source_prefix": source_prefix})
            return [{"content": "[situation] s\nl", "source": str(source_prefix)}]

    pb = tmp_path / "tool_playbook.md"
    s = MindState()
    mem = _FakeMem()
    # gate: no tool_stats → no search
    assert await tool_habits_search(mem, s, pb) == []
    assert mem.calls == []
    # gate: tool_stats present but playbook missing → no search
    s.tool_stats = {"Shell": {"ok": 1, "fail": 0}}
    assert await tool_habits_search(mem, s, pb) == []
    assert mem.calls == []
    # both present → search with source_prefix=playbook
    pb.write_text("## h\n\n[situation] s\nl\n")
    s.focus = "doing things"
    hits = await tool_habits_search(mem, s, pb)
    assert len(mem.calls) == 1
    assert mem.calls[0]["source_prefix"] == str(pb.resolve())
    assert "Shell" in mem.calls[0]["q"] and "doing things" in mem.calls[0]["q"]
    assert hits


def test_render_tool_habits_gated():
    from dollos.mind.tool_memory import render_tool_habits
    assert render_tool_habits([]) is None
    out = render_tool_habits([{"content": "## h\n\n[situation] grep\nuse Grep\n"}])
    assert "[Tool habits]" in out and "grep" in out and "use Grep" in out
```
  - `tests/test_mind_prompt.py`：
```python
def test_tool_habits_block_gated():
    from dollos.mind.mind_state import MindState
    from dollos.mind.mind_prompt import render_mind
    assert "[Tool habits]" not in render_mind(MindState(), [], "sys")
    out = render_mind(MindState(), [], "sys",
                      tool_habits_hits=[{"content": "## h\n\n[situation] grep\nuse Grep\n"}])
    assert "[Tool habits]" in out and "use Grep" in out
```
- [ ] **Step 2: 跑確認失敗** → FAIL。
- [ ] **Step 3: 實作**
  - `tool_memory.py` 新增（import `from pathlib import Path`）：
```python
def _parse_playbook_chunk(content: str) -> tuple[str, str] | None:
    """Parse a playbook entry chunk into (situation, lesson). None if unparseable."""
    situation = None
    lesson_lines: list[str] = []
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("## ") or not s:
            continue
        if s.startswith("[situation]"):
            situation = s[len("[situation]"):].strip()
        elif situation is not None:
            lesson_lines.append(s)
    if situation is None or not lesson_lines:
        return None
    return situation, " ".join(lesson_lines)


async def tool_habits_search(memsearch, state: MindState, playbook_path: Path, top_k: int = 2) -> list[dict]:
    """Retrieve top-k tool lessons relevant to recent tool use + focus.
    Gated: returns [] when there are no tool stats or no playbook file."""
    if not state.tool_stats or not playbook_path.exists():
        return []
    query = " ".join(list(state.tool_stats.keys())[:3])
    if state.focus and state.focus != "idle":
        query += " " + state.focus
    return await memsearch.search(query, top_k=top_k, source_prefix=str(playbook_path.resolve()))


def render_tool_habits(hits: list[dict]) -> str | None:
    """[Tool habits] block from playbook hits, or None when empty/unparseable."""
    if not hits:
        return None
    lines: list[str] = []
    for h in hits:
        parsed = _parse_playbook_chunk(h.get("content", ""))
        if parsed:
            lines.append(f"- [{parsed[0]}] {parsed[1]}")
    if not lines:
        return None
    return "[Tool habits]（過去學到的工具用法）：\n" + "\n".join(lines)
```
  - `mind_prompt.py` `render_mind` 新增參數 `tool_habits_hits: list[dict] | None = None`（在 `associative_hits` 之後）；在 `[Tool notes]` 附近插入（gated）：
```python
    from dollos.mind.tool_memory import render_tool_habits
    habits = render_tool_habits(tool_habits_hits or [])
    if habits:
        blocks.extend([habits, ""])
```
  - `mind_loop.iterate`：在 associative_search side-channel 之後新增：
```python
        try:
            tool_habits_hits = await tool_habits_search(
                self._ctx.memsearch, self._state,
                self._ctx.memory_root / "shared" / "tool_playbook.md",
            )
        except Exception:
            logger.exception("tool_habits_search failed; continuing without")
            tool_habits_hits = []
```
    （import `from dollos.mind.tool_memory import tool_habits_search`）並把 `tool_habits_hits=tool_habits_hits` 傳進 `render_mind`。
- [ ] **Step 4: 跑通過** — `uv run pytest tests/test_tool_memory.py tests/test_mind_prompt.py tests/test_mind_loop.py -q` → PASS。
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(mind): tool_habits_search (source_prefix) + [Tool habits] surface"`

---

## Task 8: 全套迴歸 + 整合 sanity

**Files:** 無（驗證 only）

- [ ] **Step 1: 全套** — `cd /home/progcat/Projects/DollOS && uv run pytest -q`。Expected：僅既存且不相關的 `tests/voice/test_sink.py::test_sink_fires_tts_on_text_chunk` fail（merge-base fc50522 即有），其餘全綠。
- [ ] **Step 2: grammar sanity（reflection 工具集可 build）** —
```bash
uv run python -c "
from dollos.tools import MAIN_TOOLS, REFLECTION_TOOLS
from dollos.llm.templates import build_voice_first_grammar as v
assert 'note-tool-lesson-call' not in v(MAIN_TOOLS)
assert 'note-tool-lesson-call' in v(REFLECTION_TOOLS)
print('OK: NoteToolLesson gated to REFLECTION_TOOLS grammar')
"
```
- [ ] **Step 3: Commit（若有殘留）** — `git status` → 視需要 `git add -A && git commit -m "chore: tool-memory regression fixes"`。

---

## Self-Review（plan 對 spec 覆蓋）

- §3.1 機械失敗記憶（記錄點/結構/surface/cap/校準）→ T1+T2+T3 ✅
- §3.2 NoteToolLesson reflection-only + grammar gating → T4+T5 ✅
- §3.2 reflector（is_reflection batch 偵測 + [Tool outcomes] + nudge）→ T6 ✅
- §3.2 [Tool habits]（source_prefix + gate + parse + render）→ T7 ✅
- §3.3 dispatch_one runtime-error 測試 → T3 ✅
- §4 save/load snippet → T1 ✅
- §6 subagent 隔離負向測試 → T3 ✅
- 全套迴歸 → T8 ✅
- 型別/命名一致：`record_tool_outcome`/`render_tool_notes`/`render_tool_outcomes`/`tool_habits_search`/`render_tool_habits`/`_parse_playbook_chunk`/`REFLECTION_TOOLS`/`NoteToolLesson` 跨 task 一致引用 ✅
