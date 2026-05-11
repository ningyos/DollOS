# Monitor Watcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fire-and-forget command watcher — Doll spawns long-running shell commands whose every (optionally regex-matched) stdout line fires a `MonitorTriggeredEvent` back into the dispatcher; command exit fires `MonitorExitedEvent`. Active monitors surface in a `[Active monitors]` perception block.

**Architecture:** New `MonitorRunner` mirrors `ShellRunner` / `SubagentRunner`'s fire-and-forget pattern. Each spawned monitor is an `asyncio.Task` reading `proc.stdout` line by line; on match (or unconditionally if no regex), runs rate-limit check then fires `MonitorTriggeredEvent` via `dispatch_fn`. Both event types go through `SERIALIZE_TYPES` so high-frequency triggers don't race the cascade; cascade still alive → surfaces in `[Pending events]`. `RemoveMonitor` tool kills + removes; daemon shutdown kills all. Active state rendered into a `[Active monitors]` block on every cascade iter alongside `[Pending events]`.

**Tech Stack:** Python 3.13, asyncio, pydantic, asyncio.subprocess (line-buffered streaming), structlog, pytest-asyncio.

**Spec / context:**
- Follows from step 24 (`docs/superpowers/plans/2026-05-10-external-actions-fire-and-forget.md`).
- User design (2026-05-11): "monitor 應該要跟 claude code 一樣，透過 command line return 來觸發。… GPU 溫度監控 …85 度就會通知。也可以一次性，重點是 fire-and-forget。" + "ratelimit 過了在 Active monitors 中標示觸發 rate-limit 跟頻率。"
- Naming: tool is `SpawnMonitor` (mirrors `SpawnSubagent`), runner is `MonitorRunner` (mirrors `ShellRunner`/`SubagentRunner`). The old Phase 2-3 `Monitor` (active-wait) tool no longer exists — name reuse is safe.
- The "Drone" concept (persistent agents with reasoning) is **separate** from Monitor — Drone has its own LLM cascade; Monitor is just a shell command + regex.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/dollos/events.py` | Modify | Add `MonitorTriggeredEvent` + `MonitorExitedEvent` dataclasses. |
| `src/dollos/monitor_runner.py` | Create | `MonitorRunner` class + `ActiveMonitor` dataclass; spawn watcher tasks; rate-limit; fire events; expose active-state snapshot. |
| `src/dollos/tools.py` | Modify | Add `SpawnMonitor` + `RemoveMonitor` tools; add `monitor_runner` to `ToolCtx`; add tools to `MAIN_TOOLS` / `SUB_TOOLS`. |
| `src/dollos/dispatcher.py` | Modify | `_perceive` branches for both events; `_sink_of` updates; add to `SERIALIZE_TYPES`; render `[Active monitors]` block (first user msg + per-iter inject); accept `monitor_runner` arg. |
| `src/dollos/kernel.py` | Modify | Build `MonitorRunner` before dispatcher; wire `set_dispatch_fn`; pass `monitor_runner` into dispatcher + subagent runner; `await monitor_runner.stop()` on shutdown. |
| `src/dollos/subagent.py` | Modify | Sub-cascade `ToolCtx` gets `monitor_runner` so subagents can spawn monitors too (though typically won't). |
| `src/dollos/prompts/templates/scaffolding.jinja` | Modify | Add `# Monitor` section explaining fire-and-forget pattern, rate-limit, `[Active monitors]` block. |
| `tests/test_monitor_runner.py` | Create | Unit tests for spawn / line firing / regex filter / rate-limit / exit / shutdown / remove. |
| `tests/test_tools.py` | Modify | Tests for `SpawnMonitor` delegates to runner; `RemoveMonitor` delegates; unavailable-runner paths. |
| `tests/test_dispatcher.py` | Modify | Perceive tests for both events; `[Active monitors]` block renders correctly. |
| `tests/test_kernel.py` | Modify | Wire-up assertions: `MonitorRunner` built, dispatch_fn wired, stop invoked on shutdown. |
| `tests/test_subagent.py` | Modify | Sub-cascade has `monitor_runner` in ctx. |
| `docs/roadmap.md` | Modify | Add step 25. |
| `CLAUDE.md` | Modify | Update completed plans table + 下一個. |

---

## Task 1: Add Monitor event types

**Files:**
- Modify: `src/dollos/events.py`

- [ ] **Step 1: Add MonitorTriggeredEvent and MonitorExitedEvent**

In `src/dollos/events.py`, after `ShellResultEvent`, add:

```python
@dataclass
class MonitorTriggeredEvent(RawEvent):
    """A monitor command emitted a matched stdout line.

    Fired by MonitorRunner when a watched process emits a line that
    matches the monitor's `match_regex` (or any line if regex is None),
    subject to rate-limit. Dispatcher renders this into a perception so
    Doll's cascade fires for it.

    suppressed_count: when > 0, this fire represents one matched line
        plus N suppressed-by-rate-limit lines in the prior window. Doll
        sees the number in the perception to gauge frequency.
    """

    monitor_id: str
    command: str
    line: str
    suppressed_count: int
    response_sink: asyncio.Queue[ServerMessage | None]


@dataclass
class MonitorExitedEvent(RawEvent):
    """A monitor's command exited (naturally or via RemoveMonitor).

    Always fired exactly once per monitor, regardless of rate-limit.
    `status`: 'natural' if the proc exited on its own, 'removed' if
    RemoveMonitor killed it, 'error' if the runner raised.
    """

    monitor_id: str
    command: str
    status: Literal["natural", "removed", "error"]
    exit_code: int | None
    total_matched: int
    response_sink: asyncio.Queue[ServerMessage | None]
```

`Literal` and `asyncio` are already imported (used by sibling events). No new imports needed.

- [ ] **Step 2: Run pytest to confirm no regressions**

```bash
uv run pytest -x -q
```

Expected: 321 passed (no test change yet — just additive dataclass).

- [ ] **Step 3: Commit**

```bash
git add src/dollos/events.py
git commit -m "feat(events): add MonitorTriggeredEvent and MonitorExitedEvent"
```

---

## Task 2: Create `MonitorRunner` module

**Files:**
- Create: `src/dollos/monitor_runner.py`
- Test: `tests/test_monitor_runner.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_monitor_runner.py`:

```python
"""MonitorRunner unit tests — spawn, line firing, regex, rate-limit, exit, remove."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from dollos.events import (
    MonitorExitedEvent,
    MonitorTriggeredEvent,
    RawEvent,
)
from dollos.monitor_runner import MonitorRunner


class _Capture:
    def __init__(self) -> None:
        self.events: list[RawEvent] = []

    def __call__(self, ev: RawEvent) -> None:
        self.events.append(ev)


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    """Spin until predicate() is truthy or timeout."""
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {predicate}")


@pytest.mark.asyncio
async def test_monitor_runner_fires_per_line(tmp_path: Path) -> None:
    cap = _Capture()
    runner = MonitorRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    mon_id = runner.spawn(
        command="printf 'a\\nb\\nc\\n'",
        match_regex=None,
        rate_limit_s=0,
        response_sink=sink,
    )
    assert mon_id == "mon-1"
    await _wait_for(
        lambda: any(isinstance(e, MonitorExitedEvent) for e in cap.events)
    )
    triggers = [e for e in cap.events if isinstance(e, MonitorTriggeredEvent)]
    assert [t.line for t in triggers] == ["a", "b", "c"]
    assert all(t.suppressed_count == 0 for t in triggers)
    exit_ev = [e for e in cap.events if isinstance(e, MonitorExitedEvent)][0]
    assert exit_ev.status == "natural"
    assert exit_ev.exit_code == 0
    assert exit_ev.total_matched == 3
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_regex_filters_lines(tmp_path: Path) -> None:
    cap = _Capture()
    runner = MonitorRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(
        command="printf '85\\n70\\n91\\n65\\n'",
        match_regex=r"^[89][0-9]$",
        rate_limit_s=0,
        response_sink=sink,
    )
    await _wait_for(
        lambda: any(isinstance(e, MonitorExitedEvent) for e in cap.events)
    )
    triggers = [e for e in cap.events if isinstance(e, MonitorTriggeredEvent)]
    assert [t.line for t in triggers] == ["85", "91"]
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_rate_limit_suppresses_extra_lines(
    tmp_path: Path,
) -> None:
    """With rate_limit_s very high, only the first match in the window fires."""
    cap = _Capture()
    runner = MonitorRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(
        command="printf 'hit\\nhit\\nhit\\nhit\\n'",
        match_regex=None,
        rate_limit_s=60,
        response_sink=sink,
    )
    await _wait_for(
        lambda: any(isinstance(e, MonitorExitedEvent) for e in cap.events)
    )
    triggers = [e for e in cap.events if isinstance(e, MonitorTriggeredEvent)]
    # First line fires, the next 3 are suppressed.
    assert len(triggers) == 1
    assert triggers[0].line == "hit"
    assert triggers[0].suppressed_count == 0  # first fire — no prior suppression
    exit_ev = [e for e in cap.events if isinstance(e, MonitorExitedEvent)][0]
    assert exit_ev.total_matched == 4
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_active_state_reports_suppression(
    tmp_path: Path,
) -> None:
    """active_state() exposes suppressed_in_window for [Active monitors] block."""
    cap = _Capture()
    runner = MonitorRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    # Long-running command so monitor stays active during inspection.
    runner.spawn(
        command="bash -c 'for i in 1 2 3 4 5; do echo hit; sleep 0.05; done; sleep 10'",
        match_regex=None,
        rate_limit_s=60,
        response_sink=sink,
    )
    # Wait until we've emitted some lines AND at least one fire happened.
    await _wait_for(
        lambda: any(isinstance(e, MonitorTriggeredEvent) for e in cap.events)
    )
    await asyncio.sleep(0.4)  # let the loop finish all 5 prints
    state = runner.active_state()
    assert len(state) == 1
    snap = state[0]
    assert snap["monitor_id"] == "mon-1"
    assert snap["rate_limit_s"] == 60
    assert snap["suppressed_in_window"] >= 1  # 4 lines beyond the first fire
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_remove_kills_proc_and_fires_exit(
    tmp_path: Path,
) -> None:
    cap = _Capture()
    runner = MonitorRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    mon_id = runner.spawn(
        command="sleep 30",
        match_regex=None,
        rate_limit_s=0,
        response_sink=sink,
    )
    await asyncio.sleep(0.2)
    removed = await runner.remove(mon_id)
    assert removed is True
    await _wait_for(
        lambda: any(isinstance(e, MonitorExitedEvent) for e in cap.events)
    )
    exit_ev = [e for e in cap.events if isinstance(e, MonitorExitedEvent)][0]
    assert exit_ev.status == "removed"
    assert mon_id not in {s["monitor_id"] for s in runner.active_state()}
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_remove_unknown_returns_false(tmp_path: Path) -> None:
    runner = MonitorRunner(cwd=tmp_path)
    runner.set_dispatch_fn(_Capture())
    removed = await runner.remove("mon-999")
    assert removed is False
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_stop_kills_all_active(tmp_path: Path) -> None:
    cap = _Capture()
    runner = MonitorRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(
        command="sleep 30", match_regex=None, rate_limit_s=0, response_sink=sink
    )
    runner.spawn(
        command="sleep 30", match_regex=None, rate_limit_s=0, response_sink=sink
    )
    await asyncio.sleep(0.1)
    assert len(runner.active_state()) == 2
    await runner.stop()
    assert runner.active_state() == []
```

- [ ] **Step 2: Verify tests fail (module not yet created)**

```bash
uv run pytest tests/test_monitor_runner.py -v
```

Expected: ImportError (`dollos.monitor_runner` not found).

- [ ] **Step 3: Implement MonitorRunner**

Create `src/dollos/monitor_runner.py`:

```python
"""MonitorRunner — spawn long-running shell commands; fire events per matched line.

Fire-and-forget pattern (sibling of ShellRunner / SubagentRunner). Doll
calls SpawnMonitor → MonitorRunner.spawn returns an id, kicks off an
asyncio.Task that reads proc.stdout line by line. Each line is
optionally regex-filtered, then rate-limited; surviving lines fire a
MonitorTriggeredEvent via dispatch_fn. Process exit (natural / removed /
error) always fires exactly one MonitorExitedEvent.

State surfaces to dispatcher via `active_state()` for the
`[Active monitors]` perception block; only suppression counters need to
be visible (rate-limit window, hits suppressed in last window).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dollos.events import (
    MonitorExitedEvent,
    MonitorTriggeredEvent,
    RawEvent,
)
from dollos.ipc.messages import ServerMessage

logger = logging.getLogger(__name__)


@dataclass
class ActiveMonitor:
    monitor_id: str
    command: str
    match_regex: str | None  # raw pattern string for display
    rate_limit_s: int
    started_at: datetime
    response_sink: asyncio.Queue[ServerMessage | None] | None
    # Compiled regex (None if match_regex is None).
    _compiled: re.Pattern[str] | None = None
    # Rate-limit state. window_start is when the last fire happened; None
    # means no fire yet.
    window_start: datetime | None = None
    suppressed_in_window: int = 0
    total_matched: int = 0  # total lines that passed regex (incl. suppressed)
    # Lifecycle.
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None
    remove_requested: bool = False


class MonitorRunner:
    """Spawn-and-track set of long-running monitor commands.

    Built before the dispatcher (chicken-and-egg: SpawnMonitor.run needs
    the runner; the runner needs to dispatch events into the dispatcher).
    `set_dispatch_fn` wires the sink after dispatcher build.
    """

    def __init__(
        self,
        *,
        cwd: Path,
        dispatch_fn: Callable[[RawEvent], None] | None = None,
    ) -> None:
        self._cwd = cwd
        self._dispatch_fn = dispatch_fn
        self._active: dict[str, ActiveMonitor] = {}
        self._counter = 0
        self._stopping = False

    def set_dispatch_fn(self, fn: Callable[[RawEvent], None]) -> None:
        self._dispatch_fn = fn

    def spawn(
        self,
        *,
        command: str,
        match_regex: str | None,
        rate_limit_s: int,
        response_sink: asyncio.Queue[ServerMessage | None] | None,
    ) -> str:
        """Spawn a monitor. Returns monitor_id (e.g., 'mon-1').

        Raises ValueError if match_regex is invalid.
        """
        if self._stopping:
            logger.warning("monitor spawn ignored: runner stopping")
            return ""
        compiled = re.compile(match_regex) if match_regex else None
        self._counter += 1
        monitor_id = f"mon-{self._counter}"
        mon = ActiveMonitor(
            monitor_id=monitor_id,
            command=command,
            match_regex=match_regex,
            rate_limit_s=rate_limit_s,
            started_at=datetime.now(),
            response_sink=response_sink,
            _compiled=compiled,
        )
        mon.task = asyncio.create_task(
            self._watch(mon), name=f"monitor-{monitor_id}"
        )
        self._active[monitor_id] = mon
        return monitor_id

    async def remove(self, monitor_id: str) -> bool:
        """Kill the monitor's process and remove from active set.

        Returns True if the monitor existed, False otherwise. The
        watcher task will fire MonitorExitedEvent with status='removed'
        and then exit; remove() does not await that — caller can poll
        active_state() if needed.
        """
        mon = self._active.get(monitor_id)
        if mon is None:
            return False
        mon.remove_requested = True
        if mon.proc is not None and mon.proc.returncode is None:
            try:
                os.killpg(os.getpgid(mon.proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        return True

    def active_state(self) -> list[dict[str, Any]]:
        """Snapshot for the dispatcher's [Active monitors] block."""
        out: list[dict[str, Any]] = []
        for mon in self._active.values():
            out.append({
                "monitor_id": mon.monitor_id,
                "command": mon.command,
                "match_regex": mon.match_regex,
                "rate_limit_s": mon.rate_limit_s,
                "suppressed_in_window": mon.suppressed_in_window,
            })
        return out

    async def stop(self) -> None:
        self._stopping = True
        for mon in list(self._active.values()):
            if mon.proc is not None and mon.proc.returncode is None:
                try:
                    os.killpg(os.getpgid(mon.proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if mon.task is not None and not mon.task.done():
                mon.task.cancel()
        tasks = [m.task for m in self._active.values() if m.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()

    def _fire(self, ev: RawEvent) -> None:
        if self._dispatch_fn is None:
            logger.error(
                "monitor event dropped: dispatch_fn not set "
                "(type=%s)", type(ev).__name__,
            )
            return
        try:
            self._dispatch_fn(ev)
        except Exception:
            logger.exception("dispatch_fn raised on monitor event")

    async def _watch(self, mon: ActiveMonitor) -> None:
        status: str = "natural"
        exit_code: int | None = None
        try:
            mon.proc = await asyncio.create_subprocess_shell(
                mon.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self._cwd),
                start_new_session=True,
            )
            assert mon.proc.stdout is not None
            while True:
                raw = await mon.proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if mon._compiled is not None and not mon._compiled.search(line):
                    continue
                mon.total_matched += 1
                self._consider_fire(mon, line)
            await mon.proc.wait()
            exit_code = mon.proc.returncode
            if mon.remove_requested:
                status = "removed"
        except asyncio.CancelledError:
            status = "removed"
            if mon.proc is not None and mon.proc.returncode is None:
                try:
                    os.killpg(os.getpgid(mon.proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            raise
        except Exception as e:
            logger.exception("MonitorRunner._watch unexpected error")
            status = "error"
            self._fire(MonitorExitedEvent(
                monitor_id=mon.monitor_id,
                command=mon.command,
                status="error",
                exit_code=None,
                total_matched=mon.total_matched,
                response_sink=mon.response_sink,
            ))
            self._active.pop(mon.monitor_id, None)
            return
        finally:
            # On CancelledError the finally still runs but we already
            # raised — skip event firing in that case. But this finally
            # also runs on normal exit; gate with `status` set above.
            pass
        # Natural / removed path — fire exit + drop from active set.
        self._fire(MonitorExitedEvent(
            monitor_id=mon.monitor_id,
            command=mon.command,
            status=status,
            exit_code=exit_code,
            total_matched=mon.total_matched,
            response_sink=mon.response_sink,
        ))
        self._active.pop(mon.monitor_id, None)

    def _consider_fire(self, mon: ActiveMonitor, line: str) -> None:
        now = datetime.now()
        rate = mon.rate_limit_s
        if rate <= 0:
            # No rate-limit.
            self._fire(MonitorTriggeredEvent(
                monitor_id=mon.monitor_id,
                command=mon.command,
                line=line,
                suppressed_count=0,
                response_sink=mon.response_sink,
            ))
            return
        if mon.window_start is None:
            # First fire ever.
            mon.window_start = now
            mon.suppressed_in_window = 0
            self._fire(MonitorTriggeredEvent(
                monitor_id=mon.monitor_id,
                command=mon.command,
                line=line,
                suppressed_count=0,
                response_sink=mon.response_sink,
            ))
            return
        elapsed = (now - mon.window_start).total_seconds()
        if elapsed >= rate:
            # Window expired — fire and report what was suppressed.
            fired_suppressed = mon.suppressed_in_window
            mon.window_start = now
            mon.suppressed_in_window = 0
            self._fire(MonitorTriggeredEvent(
                monitor_id=mon.monitor_id,
                command=mon.command,
                line=line,
                suppressed_count=fired_suppressed,
                response_sink=mon.response_sink,
            ))
            return
        # Within current window — suppress.
        mon.suppressed_in_window += 1
```

**Note on the `finally` block:** the implementation above intentionally lets natural exit flow past the `finally:` (which is a no-op) into the unconditional fire. CancelledError raises before reaching the unconditional fire, so the `remove`/cancel path needs its own fire — but per the design, `remove()` sets `remove_requested=True` and SIGKILLs the proc. The watcher then exits the readline loop on EOF, hits `proc.wait()`, sees `remove_requested=True`, and fires status="removed" through the natural path. The CancelledError path is reserved for `stop()` (daemon shutdown) — we don't fire events on shutdown (kernel is going away).

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_monitor_runner.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest -x -q
```

Expected: 328 passed (321 + 7).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/monitor_runner.py tests/test_monitor_runner.py
git commit -m "feat(monitor): add MonitorRunner — fire-and-forget watcher with rate-limit"
```

---

## Task 3: Add SpawnMonitor and RemoveMonitor tools

**Files:**
- Modify: `src/dollos/tools.py`
- Test: `tests/test_tools.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tools.py`:

```python
@pytest.mark.asyncio
async def test_spawn_monitor_delegates_to_runner(tmp_path):
    from dollos.tools import SpawnMonitor, ToolCtx
    from unittest.mock import MagicMock

    runner = MagicMock()
    runner.spawn.return_value = "mon-1"
    sink: asyncio.Queue = asyncio.Queue()
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path,
        memsearch=MagicMock(),
        transcripts_root=tmp_path,
        monitor_runner=runner,
    )
    out = await SpawnMonitor(
        command="tail -F /var/log/x",
        match_regex=r"ERROR",
        rate_limit_s=60,
    ).run(ctx)
    runner.spawn.assert_called_once_with(
        command="tail -F /var/log/x",
        match_regex=r"ERROR",
        rate_limit_s=60,
        response_sink=sink,
    )
    assert "mon-1" in out


@pytest.mark.asyncio
async def test_spawn_monitor_unavailable_when_no_runner(tmp_path):
    from dollos.tools import SpawnMonitor, ToolCtx
    from unittest.mock import MagicMock

    ctx = ToolCtx(
        sink=asyncio.Queue(),
        memory_root=tmp_path,
        memsearch=MagicMock(),
        transcripts_root=tmp_path,
        monitor_runner=None,
    )
    out = await SpawnMonitor(
        command="echo hi", match_regex=None, rate_limit_s=0
    ).run(ctx)
    assert "unavailable" in out.lower()


@pytest.mark.asyncio
async def test_remove_monitor_delegates(tmp_path):
    from dollos.tools import RemoveMonitor, ToolCtx
    from unittest.mock import AsyncMock, MagicMock

    runner = MagicMock()
    runner.remove = AsyncMock(return_value=True)
    ctx = ToolCtx(
        sink=asyncio.Queue(),
        memory_root=tmp_path,
        memsearch=MagicMock(),
        transcripts_root=tmp_path,
        monitor_runner=runner,
    )
    out = await RemoveMonitor(monitor_id="mon-3").run(ctx)
    runner.remove.assert_awaited_once_with("mon-3")
    assert "mon-3" in out
    assert "removed" in out.lower() or "kill" in out.lower()


@pytest.mark.asyncio
async def test_remove_monitor_unknown_id(tmp_path):
    from dollos.tools import RemoveMonitor, ToolCtx
    from unittest.mock import AsyncMock, MagicMock

    runner = MagicMock()
    runner.remove = AsyncMock(return_value=False)
    ctx = ToolCtx(
        sink=asyncio.Queue(),
        memory_root=tmp_path,
        memsearch=MagicMock(),
        transcripts_root=tmp_path,
        monitor_runner=runner,
    )
    out = await RemoveMonitor(monitor_id="mon-999").run(ctx)
    assert "mon-999" in out
    assert "unknown" in out.lower() or "not found" in out.lower()
```

- [ ] **Step 2: Run tests, verify they fail (tools don't exist, ToolCtx has no monitor_runner)**

```bash
uv run pytest tests/test_tools.py -v 2>&1 | tail -30
```

Expected: failure on imports or `ToolCtx` missing `monitor_runner`.

- [ ] **Step 3: Update tools.py**

3a. Update `ToolCtx`:

```python
@dataclass
class ToolCtx:
    """Narrow execution context passed to Tool.run().

    `subagent_runner`, `shell_runner`, and `monitor_runner` carry the
    dispatch sinks for fire-and-forget external actions. All can be None
    inside isolated test contexts; tools surface a clear "unavailable"
    message when so.
    """

    sink: asyncio.Queue[ServerMessage | None] | None
    memory_root: Path
    memsearch: MemSearch
    transcripts_root: Path
    subagent_runner: "SubagentRunner | None" = None
    subagent_report: dict | None = None
    shell_runner: "ShellRunner | None" = None
    monitor_runner: "MonitorRunner | None" = None
```

3b. Add new imports under TYPE_CHECKING (match existing pattern for `ShellRunner`):

```python
if TYPE_CHECKING:
    from dollos.monitor_runner import MonitorRunner
```

3c. Add tool classes (place after `SpawnSubagent` for symmetry):

```python
class SpawnMonitor(BaseModel):
    """Spawn a background command watcher. Returns a monitor_id immediately.

    The command runs as a long-lived subprocess. Each stdout line
    (optionally regex-filtered) fires a MonitorTriggeredEvent that
    arrives as a new turn's perception starting with 「monitor 觸發」.
    When the command exits (naturally or via RemoveMonitor), a
    MonitorExitedEvent fires.

    Rate-limit: within `rate_limit_s` seconds, at most ONE matched line
    fires an event; subsequent matches are counted as suppressed and
    surface in the [Active monitors] block (and in the next firing
    event's `suppressed_count`). Set `rate_limit_s=0` to disable.

    Examples:
        SpawnMonitor(command="nvidia-smi -l 5 --query-gpu=temperature.gpu "
                             "--format=csv,noheader",
                     match_regex=r"^[89][0-9]$", rate_limit_s=60)
        SpawnMonitor(command="tail -F /var/log/syslog",
                     match_regex=r"ERROR|CRITICAL", rate_limit_s=60)
    """

    command: str = Field(
        description="Shell command (passed to bash -c). Run long; daemon kills on shutdown.",
    )
    match_regex: str | None = Field(
        default=None,
        description=(
            "Optional Python regex. None = every line fires. Use to "
            "pre-filter inside the runner (cheaper than firing events "
            "and ignoring them)."
        ),
    )
    rate_limit_s: int = Field(
        ge=0,
        le=3600,
        description=(
            "Per-monitor seconds-between-fires window. 0 disables. "
            "60 is a reasonable default for noisy sources."
        ),
    )

    async def run(self, ctx: ToolCtx) -> str:
        if ctx.monitor_runner is None:
            return "[SpawnMonitor unavailable: no monitor_runner on this ctx]"
        try:
            monitor_id = ctx.monitor_runner.spawn(
                command=self.command,
                match_regex=self.match_regex,
                rate_limit_s=self.rate_limit_s,
                response_sink=ctx.sink,
            )
        except re.error as e:
            return f"[SpawnMonitor regex error: {e}]"
        if not monitor_id:
            return "[SpawnMonitor failed: runner is stopping]"
        return (
            f"monitor {monitor_id} dispatched "
            f"(command={self.command!r}, "
            f"match={self.match_regex!r}, "
            f"rate_limit_s={self.rate_limit_s})."
        )


class RemoveMonitor(BaseModel):
    """Kill an active monitor by id.

    The process is killed (SIGKILL on its process group). The watcher
    fires a MonitorExitedEvent with status='removed'. Active monitors
    surface in the [Active monitors] block of every cascade iter.
    """

    monitor_id: str = Field(
        description="Monitor id returned by SpawnMonitor (e.g. 'mon-3').",
    )

    async def run(self, ctx: ToolCtx) -> str:
        if ctx.monitor_runner is None:
            return "[RemoveMonitor unavailable: no monitor_runner on this ctx]"
        ok = await ctx.monitor_runner.remove(self.monitor_id)
        if ok:
            return f"monitor {self.monitor_id} kill requested."
        return f"monitor {self.monitor_id} unknown (already gone or not found)."
```

3d. Add `import re` to the file's imports (used in `SpawnMonitor.run` for `re.error`).

3e. Update `MAIN_TOOLS` and `SUB_TOOLS`:

```python
MAIN_TOOLS: list[type[BaseModel]] = [
    Say, NoteMemory, WriteDiary, WriteSchedule, Shell,
    InvokeSkill, Recall, SpawnSubagent, SpawnMonitor, RemoveMonitor,
]

SUB_TOOLS: list[type[BaseModel]] = [
    Shell, NoteMemory, Recall, InvokeSkill, Report,
    SpawnMonitor, RemoveMonitor,
]
```

(Subagents get monitor tools too — useful for ephemeral background watchers spawned inside a sub-task.)

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_tools.py -v
```

Expected: all pass (including 4 new monitor tool tests).

- [ ] **Step 5: Commit**

```bash
git add src/dollos/tools.py tests/test_tools.py
git commit -m "feat(tools): SpawnMonitor + RemoveMonitor delegating to MonitorRunner"
```

---

## Task 4: Dispatcher perceives Monitor events + renders `[Active monitors]`

**Files:**
- Modify: `src/dollos/dispatcher.py`
- Test: `tests/test_dispatcher.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_dispatcher.py`:

```python
@pytest.mark.asyncio
async def test_dispatcher_perceives_monitor_triggered(tmp_path: Path):
    from dollos.events import MonitorTriggeredEvent

    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    sink: asyncio.Queue = asyncio.Queue()
    ev = MonitorTriggeredEvent(
        monitor_id="mon-1",
        command="nvidia-smi -l 5",
        line="91",
        suppressed_count=4,
        response_sink=sink,
    )
    doll_event = await dispatcher._perceive(ev)
    p = doll_event.perception
    assert "mon-1" in p
    assert "nvidia-smi -l 5" in p
    assert "91" in p
    assert "suppressed" in p.lower() or "壓" in p
    assert "4" in p


@pytest.mark.asyncio
async def test_dispatcher_perceives_monitor_exited(tmp_path: Path):
    from dollos.events import MonitorExitedEvent

    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    sink: asyncio.Queue = asyncio.Queue()
    ev = MonitorExitedEvent(
        monitor_id="mon-2",
        command="tail -F /var/log/syslog",
        status="removed",
        exit_code=None,
        total_matched=17,
        response_sink=sink,
    )
    doll_event = await dispatcher._perceive(ev)
    p = doll_event.perception
    assert "mon-2" in p
    assert "removed" in p or "已停止" in p or "kill" in p.lower()
    assert "17" in p


def test_format_active_monitors_block():
    """Pure formatting check — given a snapshot, render the block."""
    from dollos.dispatcher import _format_active_monitors_block

    snap = [
        {
            "monitor_id": "mon-1",
            "command": "nvidia-smi -l 5",
            "match_regex": r"^[89][0-9]$",
            "rate_limit_s": 60,
            "suppressed_in_window": 11,
        },
        {
            "monitor_id": "mon-2",
            "command": "tail -F /var/log/syslog",
            "match_regex": None,
            "rate_limit_s": 0,
            "suppressed_in_window": 0,
        },
    ]
    block = _format_active_monitors_block(snap)
    assert "[Active monitors]" in block
    assert "mon-1" in block and "nvidia-smi" in block
    assert "11" in block  # suppressed count
    assert "mon-2" in block
    # Empty snapshot returns empty string.
    assert _format_active_monitors_block([]) == ""
```

- [ ] **Step 2: Run new tests, verify they fail**

```bash
uv run pytest tests/test_dispatcher.py::test_dispatcher_perceives_monitor_triggered tests/test_dispatcher.py::test_dispatcher_perceives_monitor_exited tests/test_dispatcher.py::test_format_active_monitors_block -v
```

Expected: failures (perceive lacks branches; `_format_active_monitors_block` doesn't exist).

- [ ] **Step 3: Update dispatcher.py**

3a. Imports: add `MonitorTriggeredEvent` and `MonitorExitedEvent` to the `from dollos.events import (...)` block.

3b. Add `MonitorTriggeredEvent` and `MonitorExitedEvent` to `SERIALIZE_TYPES`:

```python
SERIALIZE_TYPES: tuple[type, ...] = (
    UserTextEvent, ScheduledEvent, DailyPlanEvent, DiaryEvent,
    MonitorTriggeredEvent, MonitorExitedEvent,
)
```

3c. Add module-level helper near `_format_now`:

```python
def _format_active_monitors_block(snapshot: list[dict]) -> str:
    """Render the [Active monitors] block from MonitorRunner.active_state()."""
    if not snapshot:
        return ""
    lines: list[str] = ["[Active monitors]"]
    for s in snapshot:
        match_part = (
            f" (match: {s['match_regex']!r})" if s["match_regex"] else ""
        )
        rl = s["rate_limit_s"]
        if rl > 0:
            sup = s["suppressed_in_window"]
            if sup > 0:
                rl_part = f" [rate-limit: {rl}s, suppressed {sup} in last {rl}s]"
            else:
                rl_part = f" [rate-limit: {rl}s]"
        else:
            rl_part = ""
        lines.append(
            f"- {s['monitor_id']}: {s['command']}{match_part}{rl_part}"
        )
    return "\n".join(lines) + "\n\n"
```

3d. Add `MonitorRunner` to dispatcher constructor:

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
    monitor_runner: "MonitorRunner | None" = None,
    cascade_logger: CascadeLogger | None = None,
) -> None:
    ...
    self._monitor_runner = monitor_runner
```

Add the matching TYPE_CHECKING import for `MonitorRunner`.

3e. In `_respond`, after `recent_activity` and before `memory_block`, render the active-monitors block:

```python
recent_activity = self._format_recent_activity()
if self._monitor_runner is not None:
    active_monitors_block = _format_active_monitors_block(
        self._monitor_runner.active_state()
    )
else:
    active_monitors_block = ""
if recall_text:
    memory_block = f"[Memory context]\n{recall_text}\n\n"
else:
    memory_block = "[Memory context]\n(no relevant memory)\n\n"
first_user = (
    _format_now(datetime.now())
    + self._format_mood()
    + self._format_pending()
    + active_monitors_block
    + recent_activity
    + memory_block
    + f"[Message]\n{doll_event.perception}"
)
```

3f. In the per-iter pending-events re-injection (the existing `if iter_num > 1:` block), append active-monitors block too:

```python
if iter_num > 1:
    pending_block = self._format_pending()
    if pending_block:
        messages.append({
            "role": "user",
            "content": pending_block.rstrip(),
        })
    if self._monitor_runner is not None:
        active_block = _format_active_monitors_block(
            self._monitor_runner.active_state()
        )
        if active_block:
            messages.append({
                "role": "user",
                "content": active_block.rstrip(),
            })
```

3g. Update `ToolCtx(...)` construction to pass `monitor_runner=self._monitor_runner`.

3h. Add `_perceive` branches (after `ShellResultEvent`):

```python
if isinstance(raw, MonitorTriggeredEvent):
    extra = (
        f"（過去 window 內被 rate-limit 壓住的命中數: {raw.suppressed_count}）"
        if raw.suppressed_count > 0 else ""
    )
    perception = (
        f"monitor {raw.monitor_id} 觸發：\n"
        f"- command: {raw.command}\n"
        f"- line: {raw.line}\n"
        f"{extra}".rstrip()
    )
    return DollEvent(perception=perception, raw=raw)
if isinstance(raw, MonitorExitedEvent):
    perception = (
        f"monitor {raw.monitor_id} 結束（status={raw.status}, "
        f"exit={raw.exit_code}, 共觸發 {raw.total_matched} 行）：\n"
        f"- command: {raw.command}"
    )
    return DollEvent(perception=perception, raw=raw)
```

3i. Add both events to `_sink_of`'s isinstance tuple.

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_dispatcher.py -v 2>&1 | tail -30
```

Expected: all dispatcher tests pass.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest -x -q
```

Expected: 335 passed (328 + 3 new dispatcher tests + 4 monitor tool tests already counted earlier — adjust to actual).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/dispatcher.py tests/test_dispatcher.py
git commit -m "feat(dispatcher): perceive Monitor events + render [Active monitors] block"
```

---

## Task 5: Wire MonitorRunner into kernel + subagent

**Files:**
- Modify: `src/dollos/kernel.py`
- Modify: `src/dollos/subagent.py`
- Test: `tests/test_kernel.py`, `tests/test_subagent.py`

- [ ] **Step 1: Update kernel.py**

1a. Add import:
```python
from dollos.monitor_runner import MonitorRunner
```

1b. After `self.shell_runner = ShellRunner(...)`, add:
```python
self.monitor_runner = MonitorRunner(cwd=settings.data.root)
```

1c. Pass `monitor_runner=self.monitor_runner` into both `SubagentRunner(...)` and `EventDispatcher(...)` kwargs.

1d. After `self.shell_runner.set_dispatch_fn(self.dispatcher.dispatch)`, add:
```python
self.monitor_runner.set_dispatch_fn(self.dispatcher.dispatch)
```

1e. In the shutdown method, after `await self.shell_runner.stop()`, add:
```python
await self.monitor_runner.stop()
```

- [ ] **Step 2: Update subagent.py**

2a. Add `MonitorRunner` TYPE_CHECKING import.

2b. Add `monitor_runner: "MonitorRunner | None" = None` parameter to `SubagentRunner.__init__`; store as `self._monitor_runner`.

2c. In `_run_cascade` where it builds the sub-cascade `ToolCtx`, add `monitor_runner=self._monitor_runner`.

- [ ] **Step 3: Update kernel + subagent tests**

3a. In `tests/test_kernel.py` — find the existing kernel wiring tests for `shell_runner`. Add parallel assertions for `monitor_runner`:
- It's built.
- `set_dispatch_fn` is called.
- `stop()` is invoked on shutdown.

Pattern: copy each existing `shell_runner` wire-up assertion, add an identical one for `monitor_runner`.

3b. In `tests/test_subagent.py` — find `test_subagent_ctx_has_shell_runner`. Add a parallel `test_subagent_ctx_has_monitor_runner`:

```python
@pytest.mark.asyncio
async def test_subagent_ctx_has_monitor_runner(tmp_path: Path):
    """Subagents receive the daemon's monitor_runner via their sub-cascade ctx."""
    # Reuse the same fixture pattern as test_subagent_ctx_has_shell_runner —
    # replace the assertion with one on `monitor_runner`.
    ...
```

Read the existing `test_subagent_ctx_has_shell_runner` and clone it verbatim, replacing `shell_runner` with `monitor_runner`.

- [ ] **Step 4: Run full suite**

```bash
uv run pytest -x -q
```

Expected: all pass (kernel + subagent updates green).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(daemon): wire MonitorRunner through kernel + subagent runner"
```

---

## Task 6: Update scaffolding prompt

**Files:**
- Modify: `src/dollos/prompts/templates/scaffolding.jinja`

- [ ] **Step 1: Locate the existing external-actions explanation**

```bash
grep -n -i "shell\|fire-and-forget\|外部動作" src/dollos/prompts/templates/scaffolding.jinja
```

This identifies where the current Shell/Subagent fire-and-forget explanation lives.

- [ ] **Step 2: Add Monitor coverage to the same section**

Edit the existing bullet (currently mentions Shell + SpawnSubagent) to include Monitor. Replace the existing line:

```
- 外部動作（Shell、SpawnSubagent）是 fire-and-forget。...
```

with:

```
- 外部動作（Shell、SpawnSubagent、SpawnMonitor）都是 fire-and-forget。你 call 之後立刻拿到「派發成功」訊息，實際結果以**新的 user perception** 回來：Shell 是「你執行的 shell 命令回來了」，Subagent 是「你派出的 subagent 回來了」，Monitor 觸發是「monitor mon-N 觸發」、Monitor 結束是「monitor mon-N 結束」。沒有 wait 工具——你只要選擇繼續做事或停下來等就好。
- **Monitor** 是長跑命令的 watcher：每行 stdout（可選 regex 過濾）會 fire 一個事件。用在 GPU 溫度監控、log tail、檔案系統 watch 等。`rate_limit_s` 控制每 N 秒最多 fire 一次同個 monitor 的事件（剩下的累計成 `suppressed` 數字）；噪音大的來源建議 60s 起跳。
- `[Active monitors]` block 列出當前所有 monitor 跟它們的 suppression 狀態——看到 suppressed 數字很高就考慮 RemoveMonitor 或重新 SpawnMonitor 加更嚴格的 match_regex。
```

(If the existing line wording differs slightly, preserve its spirit while inserting the Monitor parts.)

- [ ] **Step 3: Run full suite (template tests catch any rendering breakage)**

```bash
uv run pytest -x -q
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/dollos/prompts/templates/scaffolding.jinja
git commit -m "docs(prompt): explain Monitor (SpawnMonitor / RemoveMonitor / Active monitors)"
```

---

## Task 7: E2E smoke

**Files:**
- Create: `/tmp/smoke_monitor.py` (smoke script — not committed)

- [ ] **Step 1: Start daemon**

In the worktree shell:

```bash
cp ../../config.toml config.toml
uv run python -m dollos --config config.toml > /tmp/dollos_smoke.log 2>&1 &
echo $! > /tmp/dollos_smoke.pid
sleep 4
ss -tlnp 2>/dev/null | grep :9876
```

- [ ] **Step 2: Write the smoke**

`/tmp/smoke_monitor.py`:

```python
"""Smoke: SpawnMonitor + match_regex + rate-limit + RemoveMonitor + ExitedEvent."""
import asyncio
import json
import sys

import websockets


async def collect(ws, label, timeout=180.0):
    out = []
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        t = msg.get("type")
        if t == "text_chunk":
            sys.stdout.write(msg["text"]); sys.stdout.flush()
            out.append(msg["text"])
        elif t == "turn_end":
            print(f"\n--- [{label}] turn_end ---", flush=True)
            return "".join(out)
        elif t == "error":
            print(f"\n!!! [{label}] ERROR {msg.get('message')}", flush=True)


async def main():
    async with websockets.connect("ws://127.0.0.1:9876") as ws:
        # Turn 1: spawn a monitor that emits 'hit' every 0.5s for 5 lines,
        # then exits. No regex, no rate-limit. We expect 5 trigger turns +
        # 1 exit turn (or fewer if Doll batches the cascade).
        await ws.send(json.dumps({
            "type": "text_input",
            "text": (
                "用 SpawnMonitor 開一個 monitor，command 是 "
                "'for i in 1 2 3; do echo hit-$i; sleep 0.3; done', "
                "match_regex None, rate_limit_s 0。spawn 完跟我說 id。"
            ),
        }))
        await collect(ws, "T1")

        # The monitor will fire 3 triggered turns + 1 exited turn.
        for i in range(4):
            await collect(ws, f"T-monitor-{i+1}", timeout=30.0)


asyncio.run(main())
```

- [ ] **Step 3: Run smoke**

```bash
uv run python /tmp/smoke_monitor.py
```

Expected: Doll spawns monitor, Says `mon-1`, then 3 trigger turns where Doll reports each line, then exit turn.

- [ ] **Step 4: Inspect daemon log for Monitor events**

```bash
grep -i "monitor\|MonitorTriggered\|MonitorExited" /tmp/dollos_smoke.log | tail -30
```

Expected: events firing through dispatch_fn.

- [ ] **Step 5: Tear down**

```bash
kill "$(cat /tmp/dollos_smoke.pid)" 2>/dev/null
rm /tmp/dollos_smoke.pid /tmp/smoke_monitor.py config.toml
```

- [ ] **Step 6: No commit (smoke is throwaway).**

---

## Task 8: Docs sync

**Files:**
- Modify: `docs/roadmap.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add step 25 to roadmap.md**

Insert above the step 24 entry:

```markdown
### 25. Monitor watcher — fire-and-forget command watcher with rate-limit  ✅ Merged

**範圍**：
- 新 `MonitorRunner`（mirror Shell/Subagent runners）：spawn long-running command；逐行讀 stdout；可選 regex 過濾；per-monitor rate-limit（預設 60s window，可設定，0 = disable）
- 新 events: `MonitorTriggeredEvent`（line + suppressed_count）、`MonitorExitedEvent`（natural / removed / error + total_matched）
- 兩 events 進 `SERIALIZE_TYPES`（避免高頻 monitor 撞 cascade）
- 新 tools: `SpawnMonitor(command, match_regex, rate_limit_s)`、`RemoveMonitor(monitor_id)`；加入 MAIN_TOOLS + SUB_TOOLS
- Dispatcher 渲染 `[Active monitors]` block：顯示 mon-id、command、match、rate-limit window、suppressed count
- 每 cascade iter 重渲染（mirror `[Pending events]`）
- Subagent 也能 spawn/remove monitor（共享同個 runner）
- Scaffolding 加 Monitor 章節：fire-and-forget 模式、rate-limit 語意、[Active monitors] 解讀

**設計選擇**：
- 沒有 ListMonitors tool：active state 透過 [Active monitors] block 自動 surface
- 沒有「觸發歷史」: 只顯示 suppressed count；歷史靠 NoteMemory / 之前的 perception
- 自然退出 vs RemoveMonitor: 都走 MonitorExitedEvent，status 區分
- rate_limit window 是 per-monitor 滑動 window，不全域

**Smoke**：Doll SpawnMonitor 短命令 (3 行 echo)，收到 3 個 trigger event + 1 個 exit event。
```

Also mark "下個 step 候選" at the bottom to reflect Monitor is done.

- [ ] **Step 2: Update CLAUDE.md**

In the completed plans table:

```
| Roadmap step 25 — Monitor watcher | Merged |
```

In "下一個", remove the "真 Monitor watcher" bullet (now done). Remaining:

```
- **Voice pipeline**（基礎建設，跟 Doll 行為無關）
- **Drone**（persistent agents — 跟 Subagent 對偶；Monitor 是無大腦版，Drone 是有大腦版）
- **Wake gating** — 等 voice / drone events 進來才有 ROI
```

Add to "Key Architecture Decisions":

```
- **Monitor vs Shell vs Subagent vs Drone**: All four are external actions.
  - **Shell** — one-shot command, single result event when done.
  - **Subagent** — ephemeral sub-LLM cascade, one result event when done (Report).
  - **Monitor** — long-running command, per-line trigger events + exit event. Stateless watcher; no LLM in the loop.
  - **Drone** (future) — persistent agent with its own LLM cascade, scheduled trigger, can call tools and Report back.
```

- [ ] **Step 3: Commit**

```bash
git add docs/roadmap.md CLAUDE.md
git commit -m "docs: roadmap step 25 — Monitor watcher"
```

---

## Self-Review Checklist

- [x] **Spec coverage**:
  - Configurable per-line vs regex-filtered → `match_regex` param + filter in `_watch` (Task 2)
  - Configurable one-shot vs persistent → naturally captured by command behavior (no flag needed; both go through `MonitorExitedEvent` with status='natural')
  - `[Active monitors]` block in user message → `_format_active_monitors_block` (Task 4)
  - Rate-limit with frequency display → `_consider_fire` + suppressed_in_window field + block rendering (Tasks 2, 4)
  - `MonitorTriggeredEvent` + `MonitorExitedEvent` → Task 1
  - SpawnMonitor + RemoveMonitor tools → Task 3
  - Wiring → Task 5
  - Doll-facing prompt → Task 6
  - E2E verification → Task 7
  - Docs sync → Task 8
- [x] **No placeholders** — all code blocks complete, all bash commands concrete.
- [x] **Type consistency**:
  - `MonitorTriggeredEvent` fields: `monitor_id / command / line / suppressed_count / response_sink` — used identically in events.py, MonitorRunner._consider_fire, dispatcher._perceive
  - `MonitorExitedEvent` fields: `monitor_id / command / status / exit_code / total_matched / response_sink` — same
  - `active_state()` dict keys: `monitor_id / command / match_regex / rate_limit_s / suppressed_in_window` — used in MonitorRunner + dispatcher._format_active_monitors_block (test asserts these exact keys)
  - `MonitorRunner.spawn(*, command, match_regex, rate_limit_s, response_sink)` — keyword-only, called identically by `SpawnMonitor.run`
  - `MonitorRunner.remove(monitor_id) -> bool` — async, returns bool; called by `RemoveMonitor.run`

## Notes for Reviewer

- **Risks:**
  - High-frequency monitors with `rate_limit_s=0` can flood the dispatcher's pending queue. Each line goes through `SERIALIZE_TYPES`, so cascade won't race — but the queue can grow large. Mitigation: nudge in scaffolding ("噪音大來源建議 60s 起跳") + Doll can `RemoveMonitor` if she sees runaway suppression. Out-of-scope: a hard cap on pending queue.
  - `proc.stdout.readline()` could block on a partial line at EOF. asyncio's streams handle this — readline returns `b""` on EOF — so the watcher exits cleanly. Verified by `test_monitor_runner_fires_per_line`.
  - Regex injection by Doll: `re.compile` runs on her input. A catastrophic regex (ReDoS) could hang `_compiled.search(line)`. Acceptable trade-off; in-house Doll, no adversarial input. If concerned later: `regex` package with timeout, or sandbox.

- **Out of scope (separate plans):**
  - Drone (persistent LLM agents).
  - Per-tenant monitor quotas / global cap.
  - Monitor restart / cron-style scheduling — Doll can shell-loop with `while true`, or future Drone.
