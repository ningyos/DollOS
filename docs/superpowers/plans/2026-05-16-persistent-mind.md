# Persistent Mind Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor DollOS from per-event reactive cascade dispatcher to a single continuously-running MindLoop coroutine with a unified mutable MindState. Replace `EventDispatcher` / `ConversationHistory` / `Scratchpad` (as separate concepts) with the new architecture. Tools rebind from `ToolCtx` to `MindCtx`. Solves the IV-removal ShellResult regression at the architectural level and delivers single-consciousness / multi-task awareness / global state cognition.

**Architecture:** ONE coroutine drains a unified `PerceptionQueue` (with idle-tick timeout), syncs external state into `MindState`, renders a full prompt, calls big LLM once, parses 0..N actions, executes them (sync inline OR async fire-and-forget Dispatch), persists MindState, loops. Said-by-Doll outputs go into `recent_outputs` deque to prevent say-spam; current-active sink is resolved at Say-emit time via a daemon-level `SinkResolver`.

**Tech Stack:** Python 3.13, asyncio, pydantic, pytest. No new third-party deps. Big LLM at port 8001 (Qwen3.6 35B via llama.cpp).

**Spec:** `docs/superpowers/specs/2026-05-16-persistent-mind-design.md`.

**Prototype reference:** `experiments/persistent_mind/` — four validated scenarios live there. Plan tasks reference shapes that were prototyped.

---

## Scope check

This is a single coherent refactor (replacing dispatcher with mind_loop). Touches many files but the change is one architectural shift, not multiple independent features. Single plan is correct.

## File Structure

### Created

- `src/dollos/mind/mind_state.py` — `MindState`, `Mood` (re-export), `ActiveTask`, `PendingEvent`, `OpenLoop`, `Perception`, `OutputRecord`, `Thought` + persistence helpers (`save_atomic` / `load_or_empty`)
- `src/dollos/mind/perception_queue.py` — `PerceptionQueue` (asyncio.Queue wrapper + drain-with-timeout)
- `src/dollos/mind/mind_loop.py` — `MindLoop` class with `run()` coroutine + action executor
- `src/dollos/mind/mind_prompt.py` — `render_mind(state) -> str` + system prompt assembly
- `src/dollos/mind/mind_ctx.py` — `MindCtx` dataclass (replaces `ToolCtx`)
- `src/dollos/mind/sink_resolver.py` — `SinkResolver` callable + dummy sink
- `src/dollos/mind/__init__.py` — public API
- `tests/test_mind_state.py`
- `tests/test_perception_queue.py`
- `tests/test_mind_loop.py`
- `tests/test_mind_prompt.py`
- `tests/test_mind_persistence.py`
- `tests/test_e2e_mind_smoke.py` — adapted from existing real-LLM e2e patterns

### Modified

- `src/dollos/kernel.py` — instantiate MindLoop + MindState + PerceptionQueue + SinkResolver; remove EventDispatcher / ConversationHistory / Scratchpad constructions; remove `_active_sink_or_dummy()` etc.
- `src/dollos/tools.py` — all tool `run()` signatures change `ToolCtx` → `MindCtx`; mutate `ctx.mind_state` instead of separate `ctx.scratchpad` etc.; `Say` no longer takes `response_sink`, uses `ctx.sink_resolver()` at emit time
- `src/dollos/config.py` — add `MindConfig` (idle_interval, deque maxlens); remove `ConversationHistoryConfig` (folded)
- `src/dollos/events.py` — repurpose `RawEvent` types as `Perception` payload sources; or replace entirely with new `Perception` model from `mind_state.py`
- `src/dollos/ipc/*` — `UserSpoke` perception enters PerceptionQueue (not the old dispatcher.dispatch path)
- `src/dollos/shell_runner.py` / `subagent.py` / `monitor_runner.py` — on completion, enqueue `ToolResultArrived` / `MonitorFired` / `MonitorEnded` perceptions into PerceptionQueue (not dispatcher); drop `response_sink` parameter
- `src/dollos/schedule.py` — fires `ScheduledMoment` perception
- `src/dollos/prompts/templates/scaffolding.jinja` — rewrite for new prompt blocks (Memory context / Mind state / Active tasks / Open loops / Pending / Scratchpad / Recent perceptions / Recent outputs / Recent thoughts / Decision time)
- `src/dollos/prompts/templates/subagent_scaffolding.jinja` — subagent unchanged conceptually (single cascade) but adopt the new tool ABI changes
- Existing `src/dollos/conversation_history.py` — DELETE
- Existing `src/dollos/scratchpad.py` — DELETE (`Scratchpad` class gone; tool classes move to `src/dollos/mind/scratchpad_tools.py` or stay in `tools.py`, but mutate `ctx.mind_state.scratchpad`)
- Existing `src/dollos/dispatcher.py` — DELETE
- All tests that constructed `EventDispatcher(...)`, `ToolCtx(...)`, `ConversationHistory(...)`, `Scratchpad()` — update to new shapes
- `src/dollos/llm/templates/*.py` — verify Qwen3 template still works; B4 grammar (`docs/research/grammar_injection_techreport.md`) may need new rule for `mind_actions` array (Task 7)

---

## Task 1: MindState + supporting types + unit tests

**Files:**
- Create: `src/dollos/mind/__init__.py` (empty for now)
- Create: `src/dollos/mind/mind_state.py`
- Test: `tests/test_mind_state.py`

This task creates the data layer with NO integration into existing code. Pure dataclasses + tests.

- [ ] **Step 1: Write failing tests for MindState basics**

```python
# tests/test_mind_state.py
import time
from collections import deque

import pytest

from dollos.mind.mind_state import (
    MindState, ActiveTask, PendingEvent, OpenLoop,
    Perception, OutputRecord, Thought,
)


def test_mindstate_initial_defaults() -> None:
    s = MindState()
    assert s.focus == "idle"
    assert s.energy == 1.0
    assert s.scratchpad == ""
    assert s.iter_count == 0
    assert s.active_tasks == []
    assert s.pending_events == []
    assert s.open_loops == []
    assert len(s.recent_perceptions) == 0
    assert len(s.recent_outputs) == 0
    assert len(s.recent_thoughts) == 0


def test_deque_maxlens_default() -> None:
    s = MindState()
    assert s.recent_perceptions.maxlen == 20
    assert s.recent_outputs.maxlen == 15
    assert s.recent_thoughts.maxlen == 10


def test_deque_maxlens_configurable() -> None:
    s = MindState(
        recent_perceptions=deque(maxlen=5),
        recent_outputs=deque(maxlen=5),
        recent_thoughts=deque(maxlen=5),
    )
    for i in range(10):
        s.recent_perceptions.append(Perception(kind="IdleTick", t=float(i), data={}))
    assert len(s.recent_perceptions) == 5
    assert s.recent_perceptions[0].t == 5.0


def test_open_loop_add_remove() -> None:
    s = MindState()
    s.open_loops.append(OpenLoop(id="loop1", desc="check tmp", opened_at=time.time()))
    assert len(s.open_loops) == 1
    s.open_loops = [ol for ol in s.open_loops if ol.id != "loop1"]
    assert s.open_loops == []


def test_active_task_elapsed_s() -> None:
    started = time.time() - 5.0
    t = ActiveTask(task_id="shell-1", kind="shell", summary="ls /tmp", started_at=started)
    elapsed = t.elapsed_s
    assert 4.5 <= elapsed <= 5.5
```

- [ ] **Step 2: Run tests, verify failure**

```bash
cd /home/progcat/Projects/DollOS
uv run pytest tests/test_mind_state.py -v
```
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement mind_state.py**

```python
# src/dollos/mind/mind_state.py
"""MindState — single source of truth for Doll's continuous consciousness.

See docs/superpowers/specs/2026-05-16-persistent-mind-design.md.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ActiveTask:
    task_id: str
    kind: Literal["shell", "subagent", "monitor"]
    summary: str
    started_at: float

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.started_at


@dataclass
class PendingEvent:
    fire_at: float
    summary: str


@dataclass
class OpenLoop:
    id: str
    desc: str
    opened_at: float


@dataclass
class Perception:
    kind: Literal[
        "UserSpoke", "ToolResultArrived", "MonitorFired",
        "MonitorEnded", "ScheduledMoment", "IdleTick", "Awoke",
    ]
    t: float
    data: dict


@dataclass
class OutputRecord:
    t: float
    kind: str
    summary: str


@dataclass
class Thought:
    t: float
    text: str


# Mood: re-export from existing module (don't reinvent).
from dollos.mood import Mood  # type: ignore  # NOTE: adjust import path if Mood lives elsewhere


@dataclass
class MindState:
    mood: Mood = field(default_factory=lambda: Mood())  # adjust default per existing Mood ctor
    focus: str = "idle"
    energy: float = 1.0
    scratchpad: str = ""

    active_tasks: list[ActiveTask] = field(default_factory=list)
    pending_events: list[PendingEvent] = field(default_factory=list)
    open_loops: list[OpenLoop] = field(default_factory=list)

    recent_perceptions: deque[Perception] = field(default_factory=lambda: deque(maxlen=20))
    recent_outputs: deque[OutputRecord] = field(default_factory=lambda: deque(maxlen=15))
    recent_thoughts: deque[Thought] = field(default_factory=lambda: deque(maxlen=10))

    last_user_at: float = 0.0
    last_iter_at: float = 0.0
    iter_count: int = 0
    session_started_at: float = field(default_factory=time.time)
```

Adjust the `Mood` import to wherever the existing Mood class lives — grep for it first:

```bash
grep -rn "class Mood" src/dollos/ --include="*.py"
```

- [ ] **Step 4: Run tests, verify pass**

```bash
uv run pytest tests/test_mind_state.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/__init__.py src/dollos/mind/mind_state.py tests/test_mind_state.py
git commit -m "feat(mind): MindState + supporting types"
```

---

## Task 2: Persistence (atomic save + load + Awoke)

**Files:**
- Modify: `src/dollos/mind/mind_state.py`
- Test: `tests/test_mind_persistence.py`

Atomic write + load. Refresh some fields on load.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_mind_persistence.py
import json
import time
from pathlib import Path

import pytest

from dollos.mind.mind_state import (
    MindState, OpenLoop, Perception, save_state, load_state,
)


def test_round_trip(tmp_path: Path) -> None:
    s = MindState(focus="working on blender", iter_count=42)
    s.open_loops.append(OpenLoop(id="x", desc="d", opened_at=100.0))
    s.recent_perceptions.append(Perception(kind="UserSpoke", t=99.0, data={"text": "hi"}))
    path = tmp_path / "mind_state.json"
    save_state(s, path)
    loaded = load_state(path)
    assert loaded.focus == "working on blender"
    assert loaded.iter_count == 42
    assert len(loaded.open_loops) == 1 and loaded.open_loops[0].id == "x"
    assert len(loaded.recent_perceptions) == 1
    assert loaded.recent_perceptions[0].data["text"] == "hi"


def test_load_missing_file_returns_fresh(tmp_path: Path) -> None:
    loaded = load_state(tmp_path / "absent.json")
    assert loaded.iter_count == 0


def test_load_malformed_returns_fresh(tmp_path: Path, caplog) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    loaded = load_state(path)
    assert loaded.iter_count == 0  # falls back to fresh


def test_atomic_write_no_partial_on_crash(tmp_path: Path) -> None:
    s = MindState(focus="task A", iter_count=1)
    path = tmp_path / "mind_state.json"
    save_state(s, path)
    # Simulate: tmp file should NOT exist after successful save
    assert not (tmp_path / "mind_state.json.tmp").exists()
    assert path.exists()
    # Re-save with new state
    s.focus = "task B"
    s.iter_count = 2
    save_state(s, path)
    loaded = load_state(path)
    assert loaded.focus == "task B"


def test_energy_refreshes_on_load(tmp_path: Path) -> None:
    s = MindState(energy=0.2)
    path = tmp_path / "mind_state.json"
    save_state(s, path)
    loaded = load_state(path)
    assert loaded.energy == 1.0  # refreshed on load per spec


def test_session_started_at_refreshes_on_load(tmp_path: Path) -> None:
    s = MindState()
    old_session = s.session_started_at
    path = tmp_path / "mind_state.json"
    save_state(s, path)
    time.sleep(0.05)
    loaded = load_state(path)
    assert loaded.session_started_at > old_session  # refreshed
```

- [ ] **Step 2: Run, verify failure**

```bash
uv run pytest tests/test_mind_persistence.py -v
```

- [ ] **Step 3: Implement save_state / load_state**

Add to `src/dollos/mind/mind_state.py`:

```python
import json
import logging
from dataclasses import asdict
from pathlib import Path

logger = logging.getLogger(__name__)


def save_state(state: MindState, path: Path) -> None:
    """Atomic write via tmp file + rename."""
    payload = {
        "mood": _mood_to_dict(state.mood),
        "focus": state.focus,
        "energy": state.energy,
        "scratchpad": state.scratchpad,
        "active_tasks": [asdict(t) for t in state.active_tasks],
        "pending_events": [asdict(p) for p in state.pending_events],
        "open_loops": [asdict(ol) for ol in state.open_loops],
        "recent_perceptions": [asdict(p) for p in state.recent_perceptions],
        "recent_outputs": [asdict(o) for o in state.recent_outputs],
        "recent_thoughts": [asdict(t) for t in state.recent_thoughts],
        "last_user_at": state.last_user_at,
        "last_iter_at": state.last_iter_at,
        "iter_count": state.iter_count,
        "session_started_at": state.session_started_at,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    tmp.replace(path)  # atomic rename


def load_state(path: Path) -> MindState:
    """Load or return fresh MindState on missing/malformed file."""
    if not path.exists():
        return MindState()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        logger.error("malformed mind_state at %s: %s — starting fresh", path, e)
        return MindState()

    s = MindState()
    s.mood = _mood_from_dict(data.get("mood", {}))
    s.focus = data.get("focus", "idle")
    # energy and session_started_at REFRESH on load (per spec)
    s.energy = 1.0
    s.session_started_at = time.time()
    # Restored fields:
    s.scratchpad = data.get("scratchpad", "")
    s.active_tasks = [ActiveTask(**t) for t in data.get("active_tasks", [])]
    s.pending_events = [PendingEvent(**p) for p in data.get("pending_events", [])]
    s.open_loops = [OpenLoop(**ol) for ol in data.get("open_loops", [])]
    for p in data.get("recent_perceptions", []):
        s.recent_perceptions.append(Perception(**p))
    for o in data.get("recent_outputs", []):
        s.recent_outputs.append(OutputRecord(**o))
    for t in data.get("recent_thoughts", []):
        s.recent_thoughts.append(Thought(**t))
    s.last_user_at = data.get("last_user_at", 0.0)
    s.last_iter_at = data.get("last_iter_at", 0.0)
    s.iter_count = data.get("iter_count", 0)
    return s


def _mood_to_dict(mood) -> dict:
    # Adapt to actual Mood class shape — check src/dollos/mood.py
    return {"emotion": getattr(mood, "emotion", "calm"), "reason": getattr(mood, "reason", "")}


def _mood_from_dict(d: dict):
    # Re-create Mood from dict — adapt to actual ctor
    return Mood(emotion=d.get("emotion", "calm"), reason=d.get("reason", ""))
```

The `_mood_to_dict` / `_mood_from_dict` helpers depend on the actual `Mood` class. Inspect `src/dollos/mood.py` (or wherever it lives) and adjust.

- [ ] **Step 4: Run, verify pass**

```bash
uv run pytest tests/test_mind_persistence.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/mind_state.py tests/test_mind_persistence.py
git commit -m "feat(mind): atomic state persistence + load"
```

---

## Task 3: PerceptionQueue + idle-tick drain

**Files:**
- Create: `src/dollos/mind/perception_queue.py`
- Test: `tests/test_perception_queue.py`

Asyncio queue wrapper that drains all available perceptions until a timeout fires an IdleTick.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_perception_queue.py
import asyncio
import pytest

from dollos.mind.perception_queue import PerceptionQueue
from dollos.mind.mind_state import Perception


@pytest.mark.asyncio
async def test_put_then_drain_returns_perception() -> None:
    q = PerceptionQueue()
    p = Perception(kind="UserSpoke", t=1.0, data={"text": "hi"})
    q.put(p)
    drained = await q.drain(timeout_s=1.0)
    assert len(drained) == 1
    assert drained[0].kind == "UserSpoke"


@pytest.mark.asyncio
async def test_drain_timeout_yields_idle_tick() -> None:
    q = PerceptionQueue()
    drained = await q.drain(timeout_s=0.1)
    assert len(drained) == 1
    assert drained[0].kind == "IdleTick"


@pytest.mark.asyncio
async def test_drain_returns_all_pending() -> None:
    q = PerceptionQueue()
    q.put(Perception(kind="UserSpoke", t=1.0, data={}))
    q.put(Perception(kind="ToolResultArrived", t=2.0, data={}))
    drained = await q.drain(timeout_s=1.0)
    assert len(drained) == 2
    assert drained[0].kind == "UserSpoke"
    assert drained[1].kind == "ToolResultArrived"


@pytest.mark.asyncio
async def test_drain_does_not_block_when_perceptions_available() -> None:
    import time
    q = PerceptionQueue()
    q.put(Perception(kind="UserSpoke", t=1.0, data={}))
    start = time.time()
    drained = await q.drain(timeout_s=5.0)
    elapsed = time.time() - start
    assert len(drained) == 1
    assert elapsed < 0.1  # returned immediately, didn't wait
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Implement perception_queue.py**

```python
# src/dollos/mind/perception_queue.py
"""PerceptionQueue — unified asyncio queue for all DollOS event sources."""
from __future__ import annotations

import asyncio
import time

from dollos.mind.mind_state import Perception


class PerceptionQueue:
    """Asyncio queue that drains all pending perceptions, or yields an
    IdleTick perception after a timeout."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Perception] = asyncio.Queue()

    def put(self, perception: Perception) -> None:
        """Non-blocking enqueue (call from anywhere, including non-async)."""
        self._queue.put_nowait(perception)

    async def drain(self, *, timeout_s: float) -> list[Perception]:
        """Wait up to timeout_s for at least one perception, then drain
        any others already queued. Returns [IdleTick] if timeout fires."""
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return [Perception(kind="IdleTick", t=time.time(), data={})]
        out = [first]
        while not self._queue.empty():
            out.append(self._queue.get_nowait())
        return out
```

- [ ] **Step 4: Run, verify pass**

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/perception_queue.py tests/test_perception_queue.py
git commit -m "feat(mind): PerceptionQueue with idle-tick drain"
```

---

## Task 4: SinkResolver

**Files:**
- Create: `src/dollos/mind/sink_resolver.py`
- Test: `tests/test_sink_resolver.py`

Daemon-level callable that returns the currently-active sink, or a dummy sink.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_sink_resolver.py
import asyncio
import pytest

from dollos.mind.sink_resolver import SinkResolver, DummySink


@pytest.mark.asyncio
async def test_no_sink_returns_dummy() -> None:
    resolver = SinkResolver()
    sink = resolver()
    assert isinstance(sink, DummySink)


@pytest.mark.asyncio
async def test_register_then_resolve_returns_sink() -> None:
    resolver = SinkResolver()
    real_sink = asyncio.Queue()
    handle = resolver.register(real_sink)
    sink = resolver()
    assert sink is real_sink
    resolver.unregister(handle)
    assert isinstance(resolver(), DummySink)


@pytest.mark.asyncio
async def test_most_recent_wins() -> None:
    resolver = SinkResolver()
    sink_a = asyncio.Queue()
    sink_b = asyncio.Queue()
    resolver.register(sink_a)
    resolver.register(sink_b)
    assert resolver() is sink_b


@pytest.mark.asyncio
async def test_dummy_sink_drops_messages_silently() -> None:
    dummy = DummySink()
    # put_nowait on dummy should not raise
    dummy.put_nowait({"type": "text_chunk", "text": "hi"})
```

- [ ] **Step 2-4**: Implement + verify.

```python
# src/dollos/mind/sink_resolver.py
"""SinkResolver — daemon-level current-sink lookup for Say streaming."""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class _SinkLike(Protocol):
    def put_nowait(self, item) -> None: ...


class DummySink:
    """Drops all messages with a debug log. Used when no client connected."""
    def put_nowait(self, item) -> None:
        logger.debug("dummy sink dropped: %r", item)


class SinkResolver:
    """LIFO stack of registered sinks; resolves to most-recently-registered.
    Unregister removes from the stack."""

    def __init__(self) -> None:
        self._stack: list[_SinkLike] = []
        self._dummy = DummySink()

    def register(self, sink: _SinkLike) -> int:
        self._stack.append(sink)
        return len(self._stack) - 1  # handle

    def unregister(self, handle: int) -> None:
        # Drop by identity (handle is an index hint, but we match by object)
        # Simpler: pop the top if it matches.
        if 0 <= handle < len(self._stack):
            self._stack.pop(handle)

    def __call__(self) -> _SinkLike:
        if not self._stack:
            return self._dummy
        return self._stack[-1]
```

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/sink_resolver.py tests/test_sink_resolver.py
git commit -m "feat(mind): SinkResolver — daemon-level active-sink lookup"
```

---

## Task 5: MindCtx + tool ABI migration

**Files:**
- Create: `src/dollos/mind/mind_ctx.py`
- Modify: `src/dollos/tools.py`
- Modify: `src/dollos/mind/__init__.py` to re-export
- Modify: every test that constructs `ToolCtx(...)` — update to `MindCtx(...)`
- Modify: any module that imports `ToolCtx`

This is the largest mechanical task: every tool's `run()` method changes signature `ToolCtx` → `MindCtx`. Internal field accesses change (`ctx.scratchpad.write(x)` → `ctx.mind_state.scratchpad = x` for plain assignment with cap check; same for other field-based access).

- [ ] **Step 1: Define MindCtx**

```python
# src/dollos/mind/mind_ctx.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from memsearch import MemSearch

    from dollos.mind.mind_state import MindState
    from dollos.mind.sink_resolver import SinkResolver
    from dollos.monitor_runner import MonitorRunner
    from dollos.shell_runner import ShellRunner
    from dollos.subagent import SubagentRunner
    from dollos.tool_outputs import ToolOutputStore


@dataclass
class MindCtx:
    mind_state: "MindState"
    memsearch: "MemSearch"
    memory_root: Path
    transcripts_root: Path
    sink_resolver: "SinkResolver"
    tool_output_store: "ToolOutputStore"
    shell_runner: "ShellRunner"
    subagent_runner: "SubagentRunner"
    monitor_runner: "MonitorRunner"
```

(No `Optional` fields. Required across the board.)

- [ ] **Step 2: Write failing test for one tool migration**

Pick `Say` as the canonical test case. The new `Say.run(ctx)` must:
- Resolve sink via `ctx.sink_resolver()`
- Stream the text in chunks
- Wrap in TurnStart / TurnEnd markers
- Append an OutputRecord to `ctx.mind_state.recent_outputs`

```python
@pytest.mark.asyncio
async def test_say_writes_to_resolved_sink_and_records_output(tmp_path) -> None:
    sink = asyncio.Queue()
    resolver = SinkResolver()
    resolver.register(sink)
    state = MindState()
    ctx = _make_mind_ctx(state=state, sink_resolver=resolver, ...)
    tool = Say(text="hello")
    await tool.run(ctx)
    # sink got TurnStart + TextChunk(s) + TurnEnd
    msgs = []
    while not sink.empty():
        msgs.append(sink.get_nowait())
    assert any("hello" in str(m) for m in msgs)
    # mind state recorded the output
    assert len(state.recent_outputs) == 1
    assert state.recent_outputs[0].kind == "Say"
    assert "hello" in state.recent_outputs[0].summary
```

- [ ] **Step 3: Run, verify failure**

- [ ] **Step 4: Migrate each tool**

For each tool class in `src/dollos/tools.py`:
1. Change `async def run(self, ctx: "ToolCtx")` → `async def run(self, ctx: "MindCtx")`
2. Replace `ctx.scratchpad.X(...)` with direct MindState mutation (but keep cap checks): introduce a helper `_scratchpad_write` / `_scratchpad_append` / `_scratchpad_edit` if needed
3. Replace `ctx.sink` with `ctx.sink_resolver()` (for Say)
4. After execution, append `OutputRecord` to `ctx.mind_state.recent_outputs` (skip for `Idle`)

The tool classes to migrate:
- `Say` (sink resolution + output record)
- `NoteMemory` (memsearch write + output record; transcripts_root unchanged)
- `Recall` (memsearch search → append to `ctx.mind_state.recent_thoughts`)
- `Shell` → just enqueue spawn into ShellRunner; runner is updated separately (Task 6)
- `SpawnSubagent` / `SpawnMonitor` / `RemoveMonitor` — same pattern
- `WriteScratchpad` / `AppendScratchpad` / `EditScratchpad` / `ClearScratchpad` — mutate `ctx.mind_state.scratchpad` with cap checks
- `ReadToolOutput` / `GrepToolOutput` — use `ctx.tool_output_store` (unchanged otherwise)
- `WriteDiary` / `InvokeSkill` — adapt
- `Mood` — mutate `ctx.mind_state.mood`
- (New) `SetFocus`, `OpenLoop`, `CloseLoop`, `Idle`, `Sleep` — see Task 7

Update `MAIN_TOOLS` / `SUB_TOOLS` lists (existing) — same names, just new ABI.

- [ ] **Step 5: Update all `ToolCtx(` callsites in tests to `MindCtx(`**

```bash
grep -rn "ToolCtx(" tests/ src/ --include="*.py"
```

For each, replace with `MindCtx(...)`. Tests likely need a `_make_mind_ctx(...)` helper in `tests/_dispatcher_helpers.py` (or rename file).

- [ ] **Step 6: Run full suite — fix anything that breaks**

```bash
uv run pytest -q
```

- [ ] **Step 7: Commit**

```bash
git add src/dollos/mind/mind_ctx.py src/dollos/tools.py tests/
git commit -m "feat(mind): MindCtx replaces ToolCtx — tool ABI migration"
```

---

## Task 6: New action types (SetFocus / OpenLoop / CloseLoop / Idle / Sleep)

**Files:**
- Modify: `src/dollos/tools.py` (or new `src/dollos/mind/loop_actions.py`)
- Modify: `MAIN_TOOLS` / `SUB_TOOLS` lists
- Test: `tests/test_loop_actions.py`

These are new pydantic action classes that don't exist yet.

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.asyncio
async def test_set_focus_updates_mind_state() -> None:
    state = MindState()
    ctx = _make_mind_ctx(state=state)
    await SetFocus(text="finding line 150").run(ctx)
    assert state.focus == "finding line 150"


@pytest.mark.asyncio
async def test_open_close_loop() -> None:
    state = MindState()
    ctx = _make_mind_ctx(state=state)
    await OpenLoop(id="t1", desc="check tmp").run(ctx)
    assert len(state.open_loops) == 1
    await CloseLoop(id="t1", outcome="done").run(ctx)
    assert len(state.open_loops) == 0


@pytest.mark.asyncio
async def test_close_loop_unknown_id_logs_but_no_raise() -> None:
    state = MindState()
    ctx = _make_mind_ctx(state=state)
    await CloseLoop(id="missing", outcome="x").run(ctx)
    # no exception; loop list unchanged
    assert state.open_loops == []


@pytest.mark.asyncio
async def test_sleep_action_does_not_block() -> None:
    state = MindState()
    ctx = _make_mind_ctx(state=state)
    # Sleep is a hint to MindLoop, not a real sleep. Run returns immediately.
    import time
    start = time.time()
    await Sleep(seconds=30).run(ctx)
    assert time.time() - start < 0.1
    # Sleep should record a hint on state
    assert hasattr(state, "_sleep_until") or state.recent_outputs[-1].kind == "Sleep"
```

- [ ] **Step 2-4**: Implement.

```python
class SetFocus(BaseModel):
    """Update current focus."""
    text: str = Field(..., description="one-sentence current focus")

    async def run(self, ctx: "MindCtx") -> str:
        ctx.mind_state.focus = self.text[:200]  # cap
        return f"focus → {self.text[:60]}"


class OpenLoop(BaseModel):
    """Add a TODO commitment."""
    id: str = Field(..., description="short slug id")
    desc: str = Field(..., description="what I'm committing to follow up on")

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind.mind_state import OpenLoop as OpenLoopT
        ctx.mind_state.open_loops.append(OpenLoopT(id=self.id, desc=self.desc, opened_at=time.time()))
        return f"opened loop {self.id}"


class CloseLoop(BaseModel):
    """Mark a TODO commitment resolved."""
    id: str = Field(..., description="loop id to close")
    outcome: str = Field(..., description="how it resolved")

    async def run(self, ctx: "MindCtx") -> str:
        before = len(ctx.mind_state.open_loops)
        ctx.mind_state.open_loops = [ol for ol in ctx.mind_state.open_loops if ol.id != self.id]
        if len(ctx.mind_state.open_loops) == before:
            logger.warning("close_loop: unknown id %r — no-op", self.id)
        return f"closed loop {self.id}: {self.outcome[:60]}"


class Idle(BaseModel):
    """Explicit no-op for this iteration."""
    async def run(self, ctx: "MindCtx") -> str:
        return "idle"


class Sleep(BaseModel):
    """Hint to MindLoop to extend the next idle_interval."""
    seconds: int = Field(..., ge=1, le=3600, description="extend idle drain to N seconds")

    async def run(self, ctx: "MindCtx") -> str:
        # Set a transient hint that MindLoop reads on next drain
        ctx.mind_state._sleep_hint_until = time.time() + self.seconds
        return f"sleep {self.seconds}s"
```

Add `_sleep_hint_until: float = 0.0` to MindState.

Register in MAIN_TOOLS + SUB_TOOLS.

- [ ] **Step 5: Update test_llm_grammar.py to include new action rule ids** (per scratchpad-plan precedent).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/tools.py src/dollos/mind/mind_state.py tests/
git commit -m "feat(mind): SetFocus / OpenLoop / CloseLoop / Idle / Sleep actions"
```

---

## Task 7: MindPrompt rendering

**Files:**
- Create: `src/dollos/mind/mind_prompt.py`
- Modify: `src/dollos/prompts/templates/scaffolding.jinja` — replaced; old blocks removed
- Test: `tests/test_mind_prompt.py`

- [ ] **Step 1: Write failing tests** asserting the block structure (memory context first, then mind state, etc., per spec).

- [ ] **Step 2-4**: Implement `render_mind(state, memsearch_hits: list, system_prompt: str) -> str`.

```python
def render_mind(state: MindState, memsearch_hits: list[dict], system_prompt: str) -> str:
    blocks = [
        "[Memory context]",
        _render_memory_block(memsearch_hits),
        "",
        "[Mind state]",
        _render_mindstate_block(state),
        "",
        "[Active tasks]",
        _render_active_tasks(state.active_tasks),
        "",
        "[Open loops]",
        _render_open_loops(state.open_loops),
        "",
        "[Pending]",
        _render_pending(state.pending_events),
        "",
        "[Scratchpad]",
        state.scratchpad or "(empty)",
        "",
        "[Recent perceptions]",
        _render_perceptions(state.recent_perceptions),
        "",
        "[Recent outputs] (what you did recently — don't repeat yourself)",
        _render_outputs(state.recent_outputs),
        "",
        "[Recent thoughts]",
        _render_thoughts(state.recent_thoughts),
        "",
        "[Decision time]",
        "What do you do this iteration? Output a JSON array of 0..N actions.",
    ]
    return system_prompt + "\n\n" + "\n".join(blocks)
```

Each `_render_*` helper formats the block content. Empty cases render `(none)` / `(empty)` per spec.

- [ ] **Step 5: Rewrite scaffolding.jinja** to be the system prompt only (character identity + behavior + action vocabulary documentation). No more `[Memory context]` etc. in the jinja — those are dynamically composed by `render_mind`.

- [ ] **Step 6: Test rendering**: integration test that calls `render_mind` with a populated state and asserts all blocks appear in the right order with right content.

- [ ] **Step 7: Commit**

```bash
git add src/dollos/mind/mind_prompt.py src/dollos/prompts/templates/scaffolding.jinja tests/test_mind_prompt.py
git commit -m "feat(mind): mind_prompt renderer + new scaffolding"
```

---

## Task 8: MindLoop core + Kernel integration

**Files:**
- Create: `src/dollos/mind/mind_loop.py`
- Modify: `src/dollos/kernel.py`
- Modify: `src/dollos/shell_runner.py`, `subagent.py`, `monitor_runner.py`, `schedule.py`, `ipc/*` — all enqueue perceptions into PerceptionQueue
- Delete: `src/dollos/dispatcher.py`, `src/dollos/conversation_history.py`, `src/dollos/scratchpad.py`
- Test: `tests/test_mind_loop.py`

This is the integration task. After this, DollOS runs on MindLoop instead of EventDispatcher.

- [ ] **Step 1: Write failing test (white-box) for MindLoop's iterate method**

Test the iterate method in isolation by injecting a mocked LLM that returns a known action list. Verify state mutations.

```python
@pytest.mark.asyncio
async def test_mindloop_single_iteration_executes_say_action(tmp_path) -> None:
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"}))

    mock_llm_response = [{"action": "Say", "text": "hello"}]
    loop = MindLoop(
        state=state,
        queue=queue,
        llm=_FakeLLM(returns=mock_llm_response),
        # ... other args
    )
    await loop.iterate()

    assert state.iter_count == 1
    assert len(state.recent_outputs) == 1
    assert state.recent_outputs[0].kind == "Say"
```

- [ ] **Step 2-4**: Implement MindLoop class. Handles:
  - Drain queue (with sleep_hint_until awareness)
  - Auto-sync from ProcessRegistry / Schedule
  - Memsearch query derivation from recent_perceptions
  - Prompt rendering via render_mind
  - LLM call (use existing LLM provider/template)
  - Parse JSON action array (tolerant parser as fallback)
  - Execute each action via the tool's `run()` (now `MindCtx`-bound)
  - Record OutputRecord in recent_outputs (unless Idle)
  - Persist state
  - Catch exceptions per-action so one bad action doesn't kill the loop

- [ ] **Step 5: Update Kernel**

Replace `EventDispatcher` instantiation with `MindLoop`:

```python
class DollOS:
    def __init__(self, settings):
        # ... existing setup ...
        self._queue = PerceptionQueue()
        self._mind_state = load_state(self._data_root / "mind_state.json")
        self._sink_resolver = SinkResolver()
        self._mind_loop = MindLoop(
            state=self._mind_state,
            queue=self._queue,
            llm=self._llm,
            ...
        )

    async def run(self):
        # Fire Awoke perception
        reason = "cold_start" if self._mind_state.iter_count == 0 else "resumed"
        self._queue.put(Perception(kind="Awoke", t=time.time(), data={"reason": reason}))
        
        # Spawn mind loop
        self._mind_task = asyncio.create_task(self._mind_loop.run())
        
        # ... start IPC server etc.
        # IPC user_text → self._queue.put(UserSpoke perception)
        # WS connect → self._sink_resolver.register(sink)
```

- [ ] **Step 6: Update Runners**

Each runner gains a `perception_queue` reference and enqueues on completion:

```python
class ShellRunner:
    def __init__(self, *, perception_queue, tool_output_store):
        ...
    
    async def _run(self, command, ...):
        # ... execute shell ...
        # On completion:
        self._queue.put(Perception(
            kind="ToolResultArrived",
            t=time.time(),
            data={"tool": "shell", "task_id": ..., "command": command, "exit_code": ..., "output_id": output_id, "line_count": ...},
        ))
```

Drop `response_sink` everywhere — it's daemon-managed now.

- [ ] **Step 7: Delete obsolete modules**

```bash
git rm src/dollos/dispatcher.py
git rm src/dollos/conversation_history.py
git rm src/dollos/scratchpad.py
git rm tests/test_conversation_history.py
git rm tests/test_scratchpad.py
```

(Move scratchpad tool classes BEFORE deleting scratchpad.py — they get folded into `tools.py` or `src/dollos/mind/scratchpad_tools.py`.)

- [ ] **Step 8: Run full suite — fix breakages**

Many tests will need adaptation. Update `_make_dispatcher` → `_make_mind` helper. Replace `EventDispatcher` assertions with `MindLoop` equivalents.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat(mind): MindLoop replaces EventDispatcher — main integration"
```

---

## Task 9: B4 grammar + tolerant parser for action JSON

**Files:**
- Modify: `src/dollos/llm/templates/qwen3_thinking.py` (or wherever B4 GBNF lives)
- Test: existing `tests/test_llm_grammar.py`

The model must output `<think>...</think>` followed by a JSON array of actions. Grammar locks this shape.

- [ ] **Step 1: Define mind_actions grammar rule**

```
mind-output ::= think-block "\n" mind-actions
mind-actions ::= "[" ws (action-call (ws "," ws action-call)*)? ws "]"
action-call ::= "{" ws "\"action\"" ws ":" ws action-name ws action-args? ws "}"
action-name ::= "\"Say\"" | "\"Think\"" | "\"SetFocus\"" | "\"Mood\"" | "\"OpenLoop\"" | "\"CloseLoop\"" | "\"NoteMemory\"" | "\"Recall\"" | "\"WriteScratchpad\"" | ... | "\"Idle\"" | "\"Sleep\"" | "\"Shell\"" | "\"SpawnSubagent\"" | "\"SpawnMonitor\""
action-args ::= "," ws field (ws "," ws field)*
```

Or use pydantic schema → GBNF auto-gen if the template supports it.

- [ ] **Step 2: Tolerant parser fallback**

Sometimes the grammar may not fire (e.g., if streaming is interrupted). The parser:
1. Strip leading/trailing whitespace + code fences
2. Try `json.loads`
3. If that fails, try to find balanced `[` ... `]` substring
4. If still fails, treat entire output as a single Think action

- [ ] **Step 3: Update test_llm_grammar.py** to verify all new action rule IDs.

- [ ] **Step 4: Commit**

```bash
git add src/dollos/llm/ tests/test_llm_grammar.py
git commit -m "feat(mind): B4 grammar for mind_actions array"
```

---

## Task 10: Acceptance tests (real-LLM e2e)

**Files:**
- Adapt: `scripts/smoke_doll_*` (paging, scratchpad, conversation_history, multi-turn, multi-task)
- New: `scripts/smoke_doll_idle_sleep.py`
- New: `scripts/smoke_doll_persistence.py`
- New: `scripts/smoke_doll_mind_full.py` — full integration smoke

For each existing smoke, adapt it to the new MindLoop architecture. The prompts stay the same; the daemon construction shape changes.

- [ ] **Step 1: Confirm llama-server alive** at port 8001.

- [ ] **Step 2: Run all five existing smokes adapted to MindLoop**

Each should pass with equivalent or better behavior than the original cascade-based DollOS.

- [ ] **Step 3: Write idle-sleep smoke**

5-min run with no input. Mind should Sleep-escalate, not say-spam. Pass criteria: <2 Says, Sleep called ≥1 time.

- [ ] **Step 4: Write persistence smoke**

Run scenario, kill daemon mid-task (Shell still running), restart, verify Awoke fires with `reason=resumed`, MindState fields preserved, Shell result eventually arrives as perception (or shell was lost — log + handle).

- [ ] **Step 5: Capture latency / token / iter stats**

Compare against pre-refactor smoke runs: latency should be comparable; token efficiency improved (no full history rebuild per cascade).

- [ ] **Step 6: Commit**

```bash
git add scripts/smoke_doll_*
git commit -m "feat(mind): acceptance smokes — all e2e adapted to MindLoop"
```

---

## Final verification

- [ ] **Step 1: Full suite green**

```bash
uv run pytest -q
```

- [ ] **Step 2: All v1 acceptance criteria met** (per spec)

- [ ] **Step 3: Use superpowers:finishing-a-development-branch**
