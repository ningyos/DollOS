# External Actions = Fire-and-Forget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Realign Shell to match Subagent's fire-and-forget pattern. External actions (Shell / Subagent) run in background and re-enter the event queue when done — there is no `Await` / `Monitor` tool. "Wait" is just Doll choosing to keep her cascade alive (or end it and react to the result as a new turn).

**Architecture:** Shell becomes a fire-and-forget tool that spawns a subprocess and a background watcher task, returns immediately. When the proc exits, the watcher fires a new `ShellResultEvent` through `dispatch_fn` — exactly mirroring `SubagentRunner` → `SubagentResultEvent`. The active-wait `Monitor` tool, the `Cancel` tool, and `ProcessRegistry`'s exposure to `ToolCtx` are removed. Process lifecycle (kill on daemon shutdown / timeout) lives inside the new `ShellRunner` class instead of being exposed to Doll.

**Tech Stack:** Python 3.12, asyncio, pydantic, asyncio.subprocess, structlog, pytest-asyncio.

**Spec / context:**
- Pivot from prior 4-phase plan; supersedes `docs/superpowers/plans/2026-05-10-async-shell.md` Phases 2/3.
- User clarification (2026-05-10): "Doll 內部能力（Say/NoteMemory/Recall）不能 await；外部動作（Shell/Subagent）是別人的事，doll 思考或做其他事時都在背景進行 — 結果以 event 方式回來。awaits 應該作為 doll 的運作方式，而不是 tool。"
- Subagent pattern (`src/dollos/subagent.py` + `src/dollos/events.py:SubagentResultEvent` + `dispatcher._perceive`) is the model to follow.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/dollos/shell_runner.py` | Create | Spawn shell subprocess + watcher task; fire `ShellResultEvent` on completion; track tasks; shutdown kills stragglers. |
| `src/dollos/events.py` | Modify | Add `ShellResultEvent` dataclass. |
| `src/dollos/tools.py` | Modify | Rewrite `Shell` to delegate to `ShellRunner`; delete `Monitor`, `Cancel`; remove `process_registry` / `pending_signal` from `ToolCtx`; update `MAIN_TOOLS` / `SUB_TOOLS`. |
| `src/dollos/dispatcher.py` | Modify | Add `_perceive` branch for `ShellResultEvent`; add `_sink_of` branch; treat as parallel event (like `SubagentResultEvent`); accept `shell_runner` arg; drop `process_registry` arg; drop `_pending_signal` plumbing into `ToolCtx`. |
| `src/dollos/kernel.py` | Modify | Build `ShellRunner` before dispatcher; wire `set_dispatch_fn`; remove `ProcessRegistry`; pass `shell_runner` to dispatcher and subagent runner. |
| `src/dollos/process_registry.py` | Delete | Replaced by per-runner internal tracking. |
| `src/dollos/subagent.py` | Modify | Sub-cascade `ToolCtx` now also gets `shell_runner` so subagents can use Shell. Remove `process_registry`. |
| `src/dollos/prompts/templates/scaffolding.jinja` | Modify | Replace "async Shell + Monitor" pattern explanation with "Shell is fire-and-forget — result comes back as a new event"; remove Cancel mention. |
| `src/dollos/llm/templates.py` | (no change expected) | Confirm no Monitor/Cancel-specific grammar branches need removal — generic per-tool grammar is data-driven. |
| `tests/test_dispatcher.py` | Modify | Update expectations: no `process_registry` arg; new `shell_runner`; perceive test for `ShellResultEvent`. |
| `tests/test_tools.py` | Modify | Replace Monitor/Cancel/Shell-handle tests with new fire-and-forget Shell tests. |
| `tests/test_shell_runner.py` | Create | Unit tests for `ShellRunner` (spawn, completion, timeout, shutdown). |
| `tests/test_process_registry.py` | Delete | Module gone. |
| `tests/test_kernel.py` | Modify | Wire-up assertions: `ShellRunner` built, `dispatch_fn` set, no `ProcessRegistry`. |
| `tests/test_subagent.py` | Modify | Sub-cascade has `shell_runner` not `process_registry`. |
| `docs/roadmap.md` | Modify | Mark prior async-shell phases superseded; add this step. |
| `CLAUDE.md` | Modify | Update completed plans table + 下一個 section. |

---

## Task 1: Add `ShellResultEvent`

**Files:**
- Modify: `src/dollos/events.py`
- Test: `tests/test_events.py` (or inline in test_dispatcher; check existing layout — append to `tests/test_dispatcher.py` if no events test file exists)

- [ ] **Step 1: Verify whether `tests/test_events.py` exists**

```bash
ls tests/test_events.py 2>/dev/null || echo "no test_events.py"
```

Decision: if missing, fold the structural test into Task 3 (dispatcher perceive test) — `ShellResultEvent` is a pure dataclass and the structural fields are exercised through the dispatcher path. Skip a standalone events test file (YAGNI).

- [ ] **Step 2: Add the dataclass**

In `src/dollos/events.py`, after `SubagentResultEvent`, add:

```python
@dataclass
class ShellResultEvent(RawEvent):
    """A backgrounded Shell command completed — result re-enters event queue.

    Fired by ShellRunner when the spawned proc either exits, times out, or
    raises. Dispatcher renders this into a perception so Doll's cascade fires
    for it — same pattern as SubagentResultEvent.

    Status semantics:
        ok      — proc exited 0
        nonzero — proc exited with non-zero code (output still meaningful)
        timeout — wall-clock timeout reached; proc was killed
        error   — runner-level exception (spawn failure, decode failure)
    """

    command: str
    status: Literal["ok", "nonzero", "timeout", "error"]
    exit_code: int | None
    output: str
    response_sink: asyncio.Queue[ServerMessage | None]
```

Confirm `Literal` is already imported (it is — used by `SubagentResultEvent`). Done.

- [ ] **Step 3: Commit**

```bash
git add src/dollos/events.py
git commit -m "feat(events): add ShellResultEvent for fire-and-forget Shell results"
```

---

## Task 2: Create `ShellRunner`

**Files:**
- Create: `src/dollos/shell_runner.py`
- Test: `tests/test_shell_runner.py`

- [ ] **Step 1: Write the failing test**

`tests/test_shell_runner.py`:

```python
"""ShellRunner unit tests — spawn, completion, timeout, shutdown."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from dollos.events import RawEvent, ShellResultEvent
from dollos.shell_runner import ShellRunner


class _Capture:
    def __init__(self) -> None:
        self.events: list[RawEvent] = []

    def __call__(self, ev: RawEvent) -> None:
        self.events.append(ev)


@pytest.mark.asyncio
async def test_shell_runner_fires_event_on_completion(tmp_path: Path) -> None:
    cap = _Capture()
    runner = ShellRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(command="printf hello", timeout_s=10, response_sink=sink)
    # Drain pending tasks.
    for _ in range(50):
        if cap.events:
            break
        await asyncio.sleep(0.05)
    assert len(cap.events) == 1
    ev = cap.events[0]
    assert isinstance(ev, ShellResultEvent)
    assert ev.command == "printf hello"
    assert ev.status == "ok"
    assert ev.exit_code == 0
    assert "hello" in ev.output
    await runner.stop()


@pytest.mark.asyncio
async def test_shell_runner_timeout(tmp_path: Path) -> None:
    cap = _Capture()
    runner = ShellRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(command="sleep 5", timeout_s=1, response_sink=sink)
    for _ in range(40):
        if cap.events:
            break
        await asyncio.sleep(0.1)
    assert len(cap.events) == 1
    ev = cap.events[0]
    assert ev.status == "timeout"
    assert ev.exit_code is None
    await runner.stop()


@pytest.mark.asyncio
async def test_shell_runner_nonzero_exit(tmp_path: Path) -> None:
    cap = _Capture()
    runner = ShellRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(command="exit 7", timeout_s=10, response_sink=sink)
    for _ in range(50):
        if cap.events:
            break
        await asyncio.sleep(0.05)
    ev = cap.events[0]
    assert ev.status == "nonzero"
    assert ev.exit_code == 7
    await runner.stop()


@pytest.mark.asyncio
async def test_shell_runner_stop_cancels_running(tmp_path: Path) -> None:
    cap = _Capture()
    runner = ShellRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(command="sleep 30", timeout_s=60, response_sink=sink)
    await asyncio.sleep(0.1)
    await runner.stop()
    # No event should fire (task cancelled before completion).
    assert cap.events == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_shell_runner.py -v
```

Expected: ImportError — module not yet created.

- [ ] **Step 3: Implement `ShellRunner`**

`src/dollos/shell_runner.py`:

```python
"""ShellRunner — spawn shell subprocesses; fire ShellResultEvent on completion.

Mirrors SubagentRunner's fire-and-forget pattern. Doll's `Shell` tool calls
into this runner and returns immediately; the runner watches the proc and
emits a `ShellResultEvent` via `dispatch_fn` when the proc exits, times
out, or errors. There is no Doll-callable wait/cancel — "wait" is just
Doll keeping her cascade alive, "cancel" only happens on daemon shutdown.

Lifecycle:
    Shell.run → ctx.shell_runner.spawn(command, timeout_s, response_sink)
                → asyncio.create_task(_run(...))
                       → spawn proc, await communicate() with timeout
                       → dispatch ShellResultEvent
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from dollos.events import RawEvent, ShellResultEvent
from dollos.ipc.messages import ServerMessage

logger = logging.getLogger(__name__)


SHELL_OUTPUT_MAX_CHARS = 8000


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    half = cap // 2
    head = text[:half]
    tail = text[-half:]
    dropped = len(text) - 2 * half
    return f"{head}\n...[truncated {dropped} chars]...\n{tail}"


class ShellRunner:
    """Spawn-and-track set of background shell subprocesses.

    Built before the dispatcher (chicken-and-egg: Shell.run needs a
    runner; the runner needs to dispatch result events into the
    dispatcher). The dispatch sink is set via `set_dispatch_fn` after
    the dispatcher is built. Until set, completed shell results are
    logged and dropped (defensive — should not happen in production).
    """

    def __init__(
        self,
        *,
        cwd: Path,
        dispatch_fn: Callable[[RawEvent], None] | None = None,
    ) -> None:
        self._cwd = cwd
        self._dispatch_fn = dispatch_fn
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    def set_dispatch_fn(self, fn: Callable[[RawEvent], None]) -> None:
        self._dispatch_fn = fn

    def spawn(
        self,
        *,
        command: str,
        timeout_s: int,
        response_sink: asyncio.Queue[ServerMessage | None] | None,
    ) -> None:
        """Schedule a shell subprocess. Returns immediately."""
        if self._stopping:
            logger.warning("shell spawn ignored: runner stopping")
            return
        coro = self._run(command, timeout_s, response_sink)
        t = asyncio.create_task(coro, name=f"shell-{command[:20]!r}")
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        self._stopping = True
        if not self._tasks:
            return
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _run(
        self,
        command: str,
        timeout_s: int,
        response_sink: asyncio.Queue[ServerMessage | None] | None,
    ) -> None:
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self._cwd),
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass
                self._fire(ShellResultEvent(
                    command=command,
                    status="timeout",
                    exit_code=None,
                    output=f"[timed out after {timeout_s}s]",
                    response_sink=response_sink,
                ))
                return
            output = _truncate(
                (stdout or b"").decode("utf-8", errors="replace"),
                SHELL_OUTPUT_MAX_CHARS,
            )
            status = "ok" if proc.returncode == 0 else "nonzero"
            self._fire(ShellResultEvent(
                command=command,
                status=status,
                exit_code=proc.returncode,
                output=output,
                response_sink=response_sink,
            ))
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            raise
        except Exception as e:
            logger.exception("ShellRunner._run unexpected error")
            self._fire(ShellResultEvent(
                command=command,
                status="error",
                exit_code=None,
                output=f"[runner error: {e}]",
                response_sink=response_sink,
            ))

    def _fire(self, ev: ShellResultEvent) -> None:
        if self._dispatch_fn is None:
            logger.error(
                "ShellResultEvent dropped: dispatch_fn not set "
                "(command=%r status=%s)", ev.command, ev.status,
            )
            return
        try:
            self._dispatch_fn(ev)
        except Exception:
            logger.exception("dispatch_fn raised on ShellResultEvent")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_shell_runner.py -v
```

Expected: 4 passed. If `test_shell_runner_stop_cancels_running` flakes due to timing, add a small `await asyncio.sleep(0.05)` after `stop()` before the assertion.

- [ ] **Step 5: Commit**

```bash
git add src/dollos/shell_runner.py tests/test_shell_runner.py
git commit -m "feat(shell): add ShellRunner — fire-and-forget shell subprocess + ResultEvent"
```

---

## Task 3: Dispatcher perceives `ShellResultEvent`

**Files:**
- Modify: `src/dollos/dispatcher.py`
- Test: `tests/test_dispatcher.py`

- [ ] **Step 1: Add the failing perceive test**

Append to `tests/test_dispatcher.py`:

```python
@pytest.mark.asyncio
async def test_dispatcher_perceives_shell_result_event(make_dispatcher):
    from dollos.events import ShellResultEvent

    dispatcher = await make_dispatcher()
    sink: asyncio.Queue = asyncio.Queue()
    ev = ShellResultEvent(
        command="ls /tmp",
        status="ok",
        exit_code=0,
        output="a\nb\n",
        response_sink=sink,
    )
    doll_event = await dispatcher._perceive(ev)
    p = doll_event.perception
    assert "shell 命令" in p or "Shell 命令" in p
    assert "ls /tmp" in p
    assert "exit 0" in p
    assert "a\nb" in p
```

(`make_dispatcher` fixture must already exist — used by other tests in the file. If naming differs, adapt.)

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_dispatcher.py::test_dispatcher_perceives_shell_result_event -v
```

Expected: TypeError "no stub perceive for ShellResultEvent".

- [ ] **Step 3: Add perceive branch + sink_of branch**

In `src/dollos/dispatcher.py`:

1. Add `ShellResultEvent` to imports from `dollos.events`.
2. In `_perceive`, after the `SubagentResultEvent` branch, add:

```python
if isinstance(raw, ShellResultEvent):
    perception = (
        "你執行的 shell 命令回來了：\n"
        f"- command: {raw.command}\n"
        f"- status: {raw.status} (exit {raw.exit_code})\n"
        f"- output:\n{raw.output}"
    )
    return DollEvent(perception=perception, raw=raw)
```

3. In `_sink_of`, add `ShellResultEvent` to the tuple of types that have `response_sink`.

(Don't add to `SERIALIZE_TYPES` — like `SubagentResultEvent`, this is a result of work Doll started, so parallel handling is correct. The user's pending msg should not block her own background work returning.)

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_dispatcher.py -v
```

Expected: all dispatcher tests pass, including the new one.

- [ ] **Step 5: Commit**

```bash
git add src/dollos/dispatcher.py tests/test_dispatcher.py
git commit -m "feat(dispatcher): perceive ShellResultEvent as parallel event"
```

---

## Task 4: Rewrite `Shell` tool to delegate; delete `Monitor`, `Cancel`

**Files:**
- Modify: `src/dollos/tools.py`
- Test: `tests/test_tools.py`

- [ ] **Step 1: Replace Monitor/Cancel/handle-Shell tests with new Shell test**

Open `tests/test_tools.py`. Find every test that references `Monitor`, `Cancel`, `process_registry`, or Shell-with-handle return — delete them.

Add:

```python
@pytest.mark.asyncio
async def test_shell_tool_delegates_to_runner(tmp_path):
    """Shell.run dispatches via ShellRunner.spawn and returns immediate ack."""
    from dollos.tools import Shell, ToolCtx
    from unittest.mock import MagicMock

    runner = MagicMock()
    sink: asyncio.Queue = asyncio.Queue()
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path,
        memsearch=MagicMock(),
        transcripts_root=tmp_path,
        shell_runner=runner,
    )
    out = await Shell(command="echo hi", timeout_s=30).run(ctx)
    runner.spawn.assert_called_once_with(
        command="echo hi", timeout_s=30, response_sink=sink,
    )
    assert "shell" in out.lower()
    assert "結果" in out  # result-will-arrive language


@pytest.mark.asyncio
async def test_shell_tool_unavailable_when_no_runner(tmp_path):
    from dollos.tools import Shell, ToolCtx
    from unittest.mock import MagicMock

    ctx = ToolCtx(
        sink=asyncio.Queue(),
        memory_root=tmp_path,
        memsearch=MagicMock(),
        transcripts_root=tmp_path,
        shell_runner=None,
    )
    out = await Shell(command="echo hi", timeout_s=30).run(ctx)
    assert "unavailable" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_tools.py -v
```

Expected: errors — `ToolCtx` has no `shell_runner` field, Shell has no `timeout_s`, etc.

- [ ] **Step 3: Rewrite `Shell` and rip `Monitor` / `Cancel`**

In `src/dollos/tools.py`:

1. Update `ToolCtx`:

```python
@dataclass
class ToolCtx:
    """Narrow execution context passed to Tool.run().

    `subagent_runner` and `shell_runner` carry the dispatch sinks for
    fire-and-forget external actions. Both can be None inside isolated
    test contexts; tools surface a clear "unavailable" message when so.
    """

    sink: asyncio.Queue[ServerMessage | None] | None
    memory_root: Path
    memsearch: MemSearch
    transcripts_root: Path
    subagent_runner: "SubagentRunner | None" = None
    subagent_report: dict | None = None
    shell_runner: "ShellRunner | None" = None
```

(Remove `process_registry` and `pending_signal` fields entirely.)

2. Replace `Shell` class:

```python
class Shell(BaseModel):
    """Run a shell command in the background. Returns immediately.

    Shell is fire-and-forget. The command runs as a fresh subprocess (no
    cd persistence between calls) with the daemon's user permissions and
    cwd = data/ (the parent of memory/). stdout + stderr are merged. When
    the proc finishes, its result comes back as a NEW turn's perception
    starting with 「你執行的 shell 命令回來了」 — react to it then.

    There is no wait / monitor / cancel tool. If you start a Shell and
    keep working in the same cascade, the result may also arrive as a
    perception inserted into your next iteration. Either way: react when
    you see it.
    """

    command: str = Field(
        description="Shell command to run (passed to bash -c).",
    )
    timeout_s: int = Field(
        ge=1,
        le=600,
        description=(
            "Wall-clock seconds before the proc is killed. Estimate "
            "from the command (5 short, 60 medium, 300 long; max 600). "
            "No default — pick a number every time."
        ),
    )

    async def run(self, ctx: ToolCtx) -> str:
        if ctx.shell_runner is None:
            return (
                "[Shell unavailable: no shell_runner on this ctx]"
            )
        ctx.shell_runner.spawn(
            command=self.command,
            timeout_s=self.timeout_s,
            response_sink=ctx.sink,
        )
        return (
            f"shell dispatched (command={self.command!r}, "
            f"timeout={self.timeout_s}s). 結果完成時會以新事件回來。"
        )
```

3. Delete the `Monitor` class and the `Cancel` class entirely.

4. Delete the `MONITOR_DEFAULT_TIMEOUT_S`, `MONITOR_MAX_TIMEOUT_S`, `SHELL_OUTPUT_MAX_CHARS` constants from this file (output-truncation now lives in `shell_runner.py`).

5. Update `MAIN_TOOLS` and `SUB_TOOLS`:

```python
MAIN_TOOLS: list[type[BaseModel]] = [
    Say, NoteMemory, WriteDiary, WriteSchedule, Shell,
    InvokeSkill, Recall, SpawnSubagent,
]

SUB_TOOLS: list[type[BaseModel]] = [
    Shell, NoteMemory, Recall, InvokeSkill, Report,
]
```

6. Drop the `from dollos.process_registry import ProcessRegistry` import; replace with `from dollos.shell_runner import ShellRunner` (keep under `TYPE_CHECKING` if you prefer; current pattern is direct import).

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_tools.py -v
```

Expected: passes (the two new tests + any unaffected ones). Old Monitor/Cancel tests are gone.

- [ ] **Step 5: Commit**

```bash
git add src/dollos/tools.py tests/test_tools.py
git commit -m "refactor(tools): Shell delegates to ShellRunner; delete Monitor/Cancel"
```

---

## Task 5: Wire `ShellRunner` into dispatcher + kernel; delete `ProcessRegistry`

**Files:**
- Modify: `src/dollos/dispatcher.py`
- Modify: `src/dollos/kernel.py`
- Modify: `src/dollos/subagent.py`
- Delete: `src/dollos/process_registry.py`
- Delete: `tests/test_process_registry.py`
- Test: `tests/test_kernel.py`, `tests/test_dispatcher.py`, `tests/test_subagent.py`

- [ ] **Step 1: Update dispatcher constructor**

In `EventDispatcher.__init__`:

```python
def __init__(
    self,
    *,
    adapter: LLMAdapter,
    inner_voice: InnerVoice,
    instinct: Instinct,
    renderer: PromptRenderer,
    identity: Identity,
    memory_root: Path,
    memsearch: MemSearch,
    transcripts_root: Path,
    subagent_runner: SubagentRunner | None = None,
    shell_runner: "ShellRunner | None" = None,
    cascade_logger: CascadeLogger | None = None,
) -> None:
```

Drop `process_registry` arg. Store `self._shell_runner = shell_runner`.

In `_respond`'s `ToolCtx(...)` construction, replace `process_registry=...` and `pending_signal=...` with `shell_runner=self._shell_runner`.

Drop `_pending_signal` field entirely. In `_on_cascade_done` remove the `self._pending_signal.clear()` call. The interrupt-signal pathway is no longer needed: external results inject as new turns / parallel events; Doll never actively waits.

Add `from dollos.shell_runner import ShellRunner` to imports; remove `from dollos.process_registry import ProcessRegistry`.

- [ ] **Step 2: Update kernel build order**

In `src/dollos/kernel.py`:

1. Remove `process_registry = ProcessRegistry()` and the `process_registry=process_registry` kwarg passed into the dispatcher and subagent runner.

2. After memsearch / before subagent_runner, add:

```python
shell_runner = ShellRunner(cwd=memory_root.parent)
```

3. Pass `shell_runner=shell_runner` into the dispatcher kwargs and subagent runner kwargs.

4. After dispatcher built:

```python
shell_runner.set_dispatch_fn(dispatcher.dispatch)
```

(Same line as `subagent_runner.set_dispatch_fn(dispatcher.dispatch)`.)

5. In the shutdown path, add `await shell_runner.stop()` next to `await subagent_runner.stop()`. Drop `await process_registry.shutdown()`.

6. Drop the `from dollos.process_registry import ProcessRegistry` import; add `from dollos.shell_runner import ShellRunner`.

- [ ] **Step 3: Update subagent runner**

In `src/dollos/subagent.py`:

1. Replace the `process_registry: ProcessRegistry | None = None` constructor arg with `shell_runner: "ShellRunner | None" = None` (keep TYPE_CHECKING import shape consistent with current code).

2. In `_run_cascade` where it builds the sub-cascade `ToolCtx`, replace `process_registry=self._process_registry` with `shell_runner=self._shell_runner`.

3. Drop the `from dollos.process_registry import ProcessRegistry` import.

- [ ] **Step 4: Delete `process_registry.py` and its test**

```bash
git rm src/dollos/process_registry.py tests/test_process_registry.py
```

- [ ] **Step 5: Update kernel/subagent/dispatcher tests**

In each affected test:
- Replace any `process_registry` keyword with `shell_runner` (use a `MagicMock()` or a real `ShellRunner(cwd=tmp_path)` per test's needs).
- Drop assertions about `process_registry` lifecycle; add equivalents for `shell_runner` where appropriate (e.g., `set_dispatch_fn` was called; `stop()` invoked on shutdown).

Target tests:
- `tests/test_kernel.py` — boot path
- `tests/test_dispatcher.py` — fixture / make_dispatcher
- `tests/test_subagent.py` — sub-cascade build

- [ ] **Step 6: Run full test suite**

```bash
uv run pytest -x
```

Expected: all green. If any test asserts on the exact wording in `[Pending events]` block or interrupt behavior, evaluate whether the test still applies — interrupt-aware Monitor's tests are obsolete and should be deleted.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(daemon): wire ShellRunner; remove ProcessRegistry + interrupt signal plumbing"
```

---

## Task 6: Update scaffolding prompt

**Files:**
- Modify: `src/dollos/prompts/templates/scaffolding.jinja`
- Test: snapshot rendering manually after edit

- [ ] **Step 1: Find the current Shell/Monitor explanation**

```bash
grep -n -i "monitor\|cancel\|sh-" src/dollos/prompts/templates/scaffolding.jinja
```

Read the surrounding section.

- [ ] **Step 2: Replace the Shell block**

The new explanation (insert in the appropriate `# Behavior` section, replacing the prior async-Shell-with-Monitor paragraph):

```
- 外部動作（Shell、SpawnSubagent）是 fire-and-forget。你呼叫之後立刻拿到「派發成功」訊息，
  實際結果會以**新的 user perception**回來：Shell 結果是「你執行的 shell 命令回來了」，
  Subagent 結果是「你派出的 subagent 回來了」。沒有 wait / monitor / cancel 工具——你只
  要選擇繼續做事或停下來等就好。
- 同 turn 內可以連發多個 Shell + Subagent + Say，背景並行跑。短命令通常會在你還在思考時
  就完成、插進你下一個 iter 的 perception；長命令可能等到你這 turn 結束之後才以新 turn
  方式回來。
- 想中斷外部動作？目前不支援；只能等它跑完或 timeout。所以 Shell.timeout_s 要估準。
```

Remove any sentence that mentions `Monitor(handle=...)`, `Cancel(handle=...)`, `sh-N` handle convention, "Shell 不會 block", or "interrupt-aware Monitor early-return".

- [ ] **Step 3: Render once to confirm cleanliness**

```bash
uv run python -c "
from dollos.character import Identity
from dollos.prompts import PromptRenderer
r = PromptRenderer()
print(r.render('scaffolding', identity=Identity(self='', personality='', taboos=''), available_skills=[]))
" | head -80
```

Verify the output reads cleanly with no Monitor/Cancel references. Search the output:

```bash
uv run python -c "..." 2>&1 | grep -i "monitor\|cancel\|sh-1" || echo "clean"
```

Expected: `clean`.

- [ ] **Step 4: Commit**

```bash
git add src/dollos/prompts/templates/scaffolding.jinja
git commit -m "docs(prompt): explain Shell as fire-and-forget; drop Monitor/Cancel guidance"
```

---

## Task 7: Smoke verify end-to-end

**Files:**
- Create: `/tmp/smoke_shell_async.py` (smoke script — not committed)

- [ ] **Step 1: Start daemon (background) + observe**

In a worktree shell:

```bash
cd .worktrees/external-actions  # adjust to actual worktree dir
uv run python -m dollos --config config.toml > /tmp/dollos_smoke.log 2>&1 &
echo $! > /tmp/dollos_smoke.pid
sleep 3
```

- [ ] **Step 2: Run smoke**

```python
# /tmp/smoke_shell_async.py
"""Smoke: Doll runs Shell, gets result back as new perception.

Send: 「用 shell 看一下 /tmp 有多少檔案」.
Expect:
    Turn 1 — Doll Says something like 「我看一下」 (cascade may end here, or
             Doll may emit a follow-up Say after the shell perception slides in).
    Turn 2 — Shell result perception triggers a new turn; Doll Says the count.
"""
import asyncio
import json
import sys
import websockets


async def collect(ws, label, timeout=120.0):
    out = []
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if msg.get("type") == "text_chunk":
            sys.stdout.write(msg["text"]); sys.stdout.flush()
            out.append(msg["text"])
        elif msg.get("type") == "turn_end":
            print(f"\n--- [{label}] turn_end ---", flush=True)
            return "".join(out)
        elif msg.get("type") == "error":
            print(f"\n!!! [{label}] ERROR {msg.get('message')}", flush=True)


async def main():
    async with websockets.connect("ws://127.0.0.1:9876") as ws:
        await ws.send(json.dumps({
            "type": "text_input",
            "text": "用 shell 看一下 /tmp 目錄有幾個檔案，timeout 30 秒",
        }))
        await collect(ws, "T1")
        await collect(ws, "T2", timeout=180.0)


asyncio.run(main())
```

```bash
uv run python /tmp/smoke_shell_async.py
```

- [ ] **Step 3: Verify**

Expected console output:
- T1: Doll says something acknowledging she's checking. Cascade ends (no Monitor needed).
- T2: arrives within 60s. Doll says the file count.

If T2 never arrives:
- Inspect `/tmp/dollos_smoke.log` for "ShellResultEvent dropped: dispatch_fn not set" — kernel wiring bug.
- Inspect for `dispatch_fn raised on ShellResultEvent` — perceive / sink_of bug.

If Doll calls Shell with bogus timeout / reverts to claiming Monitor — re-check scaffolding edits actually made it into the rendered system prompt.

- [ ] **Step 4: Tear down**

```bash
kill "$(cat /tmp/dollos_smoke.pid)"
rm /tmp/dollos_smoke.pid /tmp/smoke_shell_async.py
```

- [ ] **Step 5: Commit smoke log notes (no code commit)**

If everything passed, no commit needed for this step.

---

## Task 8: Update docs

**Files:**
- Modify: `docs/roadmap.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Mark prior async-shell phases superseded in roadmap**

In `docs/roadmap.md`, find the "Step 21 / 22 / 23" entries (Schedule + Async Shell + Cancel). Add a note under each Phase 2 / Phase 3 entry:

```
> **Superseded 2026-05-10**: external actions pivoted to fire-and-forget
> via ShellRunner + ShellResultEvent. Monitor (active-wait) and Cancel
> tools removed; ProcessRegistry deleted. See
> `docs/superpowers/plans/2026-05-10-external-actions-fire-and-forget.md`.
```

Add a new step entry:

```
| Roadmap step 24 — External actions = fire-and-forget (Shell ≈ Subagent) | Merged |
```

- [ ] **Step 2: Update CLAUDE.md**

In the completed plans table:
- Add: `| Roadmap step 24 — External actions = fire-and-forget | Merged |`

In the "下一個" section, remove the Phase 4 Subagent-unify bullet (this plan replaces it). Update remaining candidates:

```
- **Voice pipeline**（基礎建設，跟 Doll 行為無關）
- **Drone**（persistent agents — 跟 Subagent 對偶；新真 Monitor watcher 是 fire-and-forget watcher 的延伸）
- **真 Monitor watcher**（fire-and-forget command runner with stdout-line-as-event；用戶原本構想的 Monitor）
- **Wake gating** — 等 voice / drone events 進來才有 ROI
```

Add to "Key Architecture Decisions":

```
- **External actions are fire-and-forget**: Shell and SpawnSubagent both spawn
  background workers and return immediately; results re-enter the event queue
  as `{Tool}ResultEvent`. There is no Doll-callable wait/cancel tool. "Wait"
  is implicit — Doll's cascade either keeps going (and the result may inject
  on a later iter) or ends (and the result triggers a new turn).
```

- [ ] **Step 3: Commit**

```bash
git add docs/roadmap.md CLAUDE.md
git commit -m "docs: roadmap step 24 — external actions fire-and-forget"
```

---

## Self-Review Checklist

- [x] Spec coverage — every requirement maps to a task:
  - Shell becomes fire-and-forget → Task 4
  - Result re-enters event queue → Tasks 1+2+3
  - Remove Monitor / Cancel → Task 4
  - Remove ProcessRegistry → Task 5
  - Subagent and Shell symmetric → Task 5 (subagent gets `shell_runner`; both follow `*_runner` + `set_dispatch_fn` pattern)
  - Prompt updated → Task 6
  - E2E verified → Task 7
  - Docs synced → Task 8
- [x] No placeholders — all code is concrete; all bash commands and expected outputs are specific.
- [x] Type consistency — `ShellResultEvent` field names (`command`, `status`, `exit_code`, `output`, `response_sink`) are referenced consistently across events.py / shell_runner.py / dispatcher._perceive.
- [x] `ShellRunner.spawn(command=, timeout_s=, response_sink=)` signature matches `Shell.run`'s call site and the test's mock assertions.
- [x] `ShellRunner.set_dispatch_fn` mirrors `SubagentRunner.set_dispatch_fn` exactly.

## Notes for Reviewer

- **Risks:**
  - Removing `_pending_signal` from `ToolCtx` simplifies `_respond` but means active cascades no longer get an explicit interrupt nudge from incoming user msgs. The `[Pending events]` block already covers this on iter ≥ 2; user msg → in-progress Shell can no longer cause early-cancel-of-Shell, but that was always weird (Shell would die without producing output). Now Shell either finishes (result becomes new turn) or times out. Acceptable.
  - `SubagentResultEvent` and `ShellResultEvent` are both parallel-handled. If a long Shell finishes mid-user-turn, its perception fires concurrently with the user turn. This was already the case for subagent; pattern is established.

- **Out of scope (separate plans):**
  - True fire-and-forget Monitor watcher (long-running command, stdout-line-as-event) — covered by future "real Monitor" step in roadmap.
  - Doll-callable cancel for in-flight Shell / Subagent — YAGNI until usage data shows need.
