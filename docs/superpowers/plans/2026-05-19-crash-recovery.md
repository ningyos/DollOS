# Plan 4: Crash Recovery — Perception WAL + dirty restart detection

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the daemon crashes (OOM kill, segfault, SIGKILL), restart without losing user-facing data. Specifically: replay perceptions that arrived but weren't yet processed by mind_loop, and let Doll know she just recovered from a crash.

**Architecture:**
- **Write-Ahead Log (WAL)** for `perception_queue`: every `put()` appends one JSONL line to `data/wal/perceptions-{YYYY-MM-DD}.jsonl`. After mind_loop successfully completes an `iterate()` (persisting mind_state), the WAL is truncated up to the consumed sequence id.
- **Dirty shutdown detection**: daemon writes a PID lockfile at startup; deletes it on clean shutdown. On startup, if the lockfile names a pid no longer running, this was a dirty restart → push an `Awoke(reason="recovered")` perception with a brief context summary.

**Tech Stack:** Python 3.13, asyncio. JSONL files on disk. No new external dependencies.

**Out of scope:**
- Active monitor revive (monitors die with daemon; restart requires Doll to re-`SpawnMonitor`). Doll sees `[Active monitors]` as empty after recovery, naturally re-spawns if she still wants the watch.
- In-flight Shell / Subagent orphan management. Their external processes either complete after daemon dies (orphaned, output lost) or get reaped by the OS. Their result events won't reach the WAL since the daemon was already dead when they would have been emitted.
- Mid-LLM-stream partial output. The cascade was aborted mid-turn; the partial Say is lost. Doll's `recent_outputs` will show the truncated speech segment (already persisted to mind_state via Plan 1's anti-spam tracking).
- Memory write transactionality — NoteMemory writes to markdown + memsearch already happen synchronously per call. Worst case on crash: a half-written markdown file with a partial bullet. Acceptable; memsearch re-indexes on next startup.

---

## File Structure

**New files:**
- `src/dollos/wal/__init__.py` — package marker
- `src/dollos/wal/perception_log.py` — `PerceptionWAL` class (append / iter_pending / truncate)
- `tests/test_perception_wal.py`
- `tests/test_crash_recovery.py` — integration test
- `scripts/smoke_crash_recovery.py` — E2E smoke

**Modified:**
- `src/dollos/mind/perception_queue.py` — `PerceptionQueue` accepts optional `wal: PerceptionWAL | None`; `put()` appends to WAL when present
- `src/dollos/mind/mind_loop.py` — after `save_state(...)`, call `wal.truncate_through(last_seq)`
- `src/dollos/kernel.py` — instantiate `PerceptionWAL` at startup; pass into PerceptionQueue; replay pending entries before mind_loop starts; write/delete PID lockfile; on dirty restart push `Awoke(reason="recovered")` with summary
- `src/dollos/mind/mind_state.py` — `last_consumed_seq: int` field for WAL coordination (or store seq inside Perception)
- `src/dollos/mind/mind_prompt.py` — render `Awoke(reason="recovered")` body differently from cold_start / resumed

---

## Task 1: PerceptionWAL writer

**Files:**
- Create: `src/dollos/wal/__init__.py`
- Create: `src/dollos/wal/perception_log.py`
- Create: `tests/test_perception_wal.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_perception_wal.py
import json
import time
from pathlib import Path

from dollos.mind.mind_state import Perception
from dollos.wal.perception_log import PerceptionWAL


def test_wal_append_and_read(tmp_path: Path):
    wal = PerceptionWAL(tmp_path / "wal" / "perceptions.jsonl")
    p1 = Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"})
    seq1 = wal.append(p1)
    p2 = Perception(kind="ScheduledMoment", t=time.time(), data={"text": "alarm"})
    seq2 = wal.append(p2)
    assert seq2 == seq1 + 1

    pending = list(wal.iter_pending())
    assert len(pending) == 2
    assert pending[0][0] == seq1
    assert pending[0][1].kind == "UserSpoke"
    assert pending[0][1].data == {"text": "hi"}
    assert pending[1][0] == seq2
    assert pending[1][1].kind == "ScheduledMoment"


def test_wal_truncate_through(tmp_path: Path):
    wal = PerceptionWAL(tmp_path / "wal" / "perceptions.jsonl")
    s1 = wal.append(Perception(kind="A", t=1.0, data={}))
    s2 = wal.append(Perception(kind="B", t=2.0, data={}))
    s3 = wal.append(Perception(kind="C", t=3.0, data={}))
    wal.truncate_through(s2)  # ack s1, s2

    pending = list(wal.iter_pending())
    assert len(pending) == 1
    assert pending[0][1].kind == "C"


def test_wal_truncate_through_all(tmp_path: Path):
    wal = PerceptionWAL(tmp_path / "wal" / "perceptions.jsonl")
    s1 = wal.append(Perception(kind="A", t=1.0, data={}))
    wal.truncate_through(s1)
    assert list(wal.iter_pending()) == []


def test_wal_creates_directory(tmp_path: Path):
    path = tmp_path / "deep" / "nested" / "wal.jsonl"
    wal = PerceptionWAL(path)
    wal.append(Perception(kind="A", t=1.0, data={}))
    assert path.exists()


def test_wal_handles_corrupt_line(tmp_path: Path):
    """Malformed JSONL lines (e.g. partial write before crash) are skipped, not crashed on."""
    path = tmp_path / "wal.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"seq": 1, "kind": "A", "t": 1.0, "data": {}}\n{"seq": 2, "ki\n{"seq": 3, "kind": "C", "t": 3.0, "data": {}}\n')

    wal = PerceptionWAL(path)
    pending = list(wal.iter_pending())
    # The corrupt middle line is dropped; we recover what we can.
    kinds = [p.kind for _, p in pending]
    assert "A" in kinds
    assert "C" in kinds


def test_wal_persists_across_instances(tmp_path: Path):
    path = tmp_path / "wal.jsonl"
    w1 = PerceptionWAL(path)
    w1.append(Perception(kind="A", t=1.0, data={}))
    w1.append(Perception(kind="B", t=2.0, data={}))

    w2 = PerceptionWAL(path)
    pending = list(w2.iter_pending())
    assert len(pending) == 2
    # Next append uses the next available seq, even with a fresh instance
    s = w2.append(Perception(kind="C", t=3.0, data={}))
    pending = list(w2.iter_pending())
    assert pending[-1][0] == s
    assert pending[-1][1].kind == "C"
```

- [ ] **Step 2: Run failing**

```
uv run pytest tests/test_perception_wal.py -v
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/dollos/wal/__init__.py
"""Write-ahead logs for crash recovery."""
```

```python
# src/dollos/wal/perception_log.py
"""PerceptionWAL — append-only JSONL log of perceptions for crash recovery.

Each line: {"seq": int, "kind": str, "t": float, "data": {...}}\n

Workflow:
  - PerceptionQueue.put() → wal.append(perception)
  - mind_loop.iterate() processes the perceptions, then
    wal.truncate_through(last_seq) removes consumed entries.
  - Daemon startup: wal.iter_pending() yields anything left unconsumed
    from the last run; kernel pushes them back into the queue.

The WAL is intentionally simple — no fsync between writes (the OS page
cache is acceptable; the worst case is losing the last few perceptions
that arrived in the final ~ms before the crash, which is acceptable for
a personal companion daemon).
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from dollos.mind.mind_state import Perception

logger = logging.getLogger(__name__)


class PerceptionWAL:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Compute next seq from existing file (recovery from last run).
        self._next_seq = self._scan_max_seq() + 1

    def _scan_max_seq(self) -> int:
        if not self._path.exists():
            return 0
        max_seq = 0
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and "seq" in obj:
                    s = int(obj["seq"])
                    if s > max_seq:
                        max_seq = s
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
        return max_seq

    def append(self, perception: Perception) -> int:
        """Append one perception to the log. Returns the sequence id."""
        seq = self._next_seq
        self._next_seq += 1
        line = json.dumps(
            {
                "seq": seq,
                "kind": perception.kind,
                "t": perception.t,
                "data": perception.data,
            },
            ensure_ascii=False,
        )
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return seq

    def iter_pending(self) -> Iterator[tuple[int, Perception]]:
        """Yield (seq, perception) for every entry currently in the log."""
        if not self._path.exists():
            return
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
                seq = int(obj["seq"])
                p = Perception(kind=obj["kind"], t=float(obj["t"]), data=obj.get("data") or {})
                yield seq, p
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                logger.warning("wal: skipping corrupt line: %s", e)
                continue

    def truncate_through(self, seq: int) -> None:
        """Remove entries with sequence id <= seq. Idempotent."""
        if not self._path.exists():
            return
        kept = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
                if int(obj["seq"]) > seq:
                    kept.append(line)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue  # drop corrupt
        if kept:
            self._path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        else:
            # Truncate to empty (keep file for the future appends)
            self._path.write_text("", encoding="utf-8")
```

- [ ] **Step 4: Pass**

```
uv run pytest tests/test_perception_wal.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/dollos/wal/__init__.py src/dollos/wal/perception_log.py tests/test_perception_wal.py
git commit -m "feat(wal): PerceptionWAL — append-only JSONL log for crash recovery"
```

---

## Task 2: PerceptionQueue WAL integration

**Files:**
- Modify: `src/dollos/mind/perception_queue.py`
- Extend: `tests/test_perception_queue.py` (if exists; otherwise create)

- [ ] **Step 1: Failing test**

```python
# tests/test_perception_queue.py — append

import time
from pathlib import Path

import pytest

from dollos.mind.mind_state import Perception
from dollos.mind.perception_queue import PerceptionQueue
from dollos.wal.perception_log import PerceptionWAL


@pytest.mark.asyncio
async def test_perception_queue_writes_wal_on_put(tmp_path: Path):
    wal = PerceptionWAL(tmp_path / "wal.jsonl")
    queue = PerceptionQueue(wal=wal)
    p1 = Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"})
    queue.put(p1)
    queue.put(Perception(kind="ScheduledMoment", t=time.time(), data={}))

    # WAL should have both entries
    pending = list(wal.iter_pending())
    assert len(pending) == 2
    assert pending[0][1].kind == "UserSpoke"


@pytest.mark.asyncio
async def test_perception_queue_no_wal_still_works(tmp_path: Path):
    """When wal=None, queue still works (backwards compat)."""
    queue = PerceptionQueue()  # no wal arg
    queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"}))
    drained = await queue.drain(timeout_s=0.5)
    assert len(drained) == 1


@pytest.mark.asyncio
async def test_perception_queue_attaches_seq_to_drained(tmp_path: Path):
    """When WAL is present, each drained perception carries its seq id."""
    wal = PerceptionWAL(tmp_path / "wal.jsonl")
    queue = PerceptionQueue(wal=wal)
    queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"}))
    queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "bye"}))

    drained = await queue.drain(timeout_s=0.5)
    seqs = [d.seq for d in drained]
    assert seqs == [1, 2] or seqs[0] < seqs[1]  # monotonic
```

(Decision: how should the seq id propagate? Two options — annotate `Perception` itself with an optional `seq` field, or wrap drained items in `(seq, perception)` tuples. Annotating Perception is simpler. Add `seq: int | None = None` to the dataclass — backwards compat preserved.)

- [ ] **Step 2: Run failing**

- [ ] **Step 3: Implement**

In `src/dollos/mind/mind_state.py`, add to `Perception` dataclass:
```python
@dataclass
class Perception:
    kind: str
    t: float
    data: dict
    seq: int | None = None  # set by PerceptionQueue when WAL is active
```

In `src/dollos/mind/perception_queue.py`:
```python
from dollos.wal.perception_log import PerceptionWAL  # type-only import OK with TYPE_CHECKING

class PerceptionQueue:
    def __init__(self, wal: "PerceptionWAL | None" = None) -> None:
        self._queue: asyncio.Queue[Perception] = asyncio.Queue()
        self._shutdown_event = asyncio.Event()
        self._wal = wal

    def put(self, perception: Perception) -> None:
        if self._wal is not None and perception.seq is None:
            perception.seq = self._wal.append(perception)
        self._queue.put_nowait(perception)
```

(Perceptions arriving via WAL replay already have `seq` set, so the `seq is None` check avoids double-logging.)

- [ ] **Step 4: Pass**

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/mind_state.py src/dollos/mind/perception_queue.py tests/test_perception_queue.py
git commit -m "feat(perception): WAL integration — auto-log puts + attach seq"
```

---

## Task 3: mind_loop truncates WAL after iter

**Files:**
- Modify: `src/dollos/mind/mind_loop.py`
- Extend: `tests/test_mind_loop.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_mind_loop.py — append

@pytest.mark.asyncio
async def test_iterate_truncates_wal_after_state_save(tmp_path):
    """After iterate() persists mind_state, WAL is truncated through the last consumed seq."""
    from dollos.wal.perception_log import PerceptionWAL
    wal = PerceptionWAL(tmp_path / "wal.jsonl")
    queue = PerceptionQueue(wal=wal)
    # Pre-populate WAL via queue.put — this generates seqs
    queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"}, seq=None))
    queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "bye"}, seq=None))

    loop = _make_mind_loop(tmp_path, queue=queue, wal=wal)
    await loop.iterate()

    # After iterate, WAL is empty
    assert list(wal.iter_pending()) == []
```

(Adapt `_make_mind_loop` to accept `wal` if it doesn't already.)

- [ ] **Step 2: Run failing**

- [ ] **Step 3: Implement**

In `src/dollos/mind/mind_loop.py`:
- `__init__` accepts `wal: PerceptionWAL | None = None`
- After `save_state(...)` in `iterate()`:
  ```python
  if self._wal is not None and perceptions:
      last_seq = max((p.seq for p in perceptions if p.seq is not None), default=None)
      if last_seq is not None:
          self._wal.truncate_through(last_seq)
  ```
- Kernel passes the wal instance when constructing MindLoop.

- [ ] **Step 4: Pass**

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/mind_loop.py tests/test_mind_loop.py
git commit -m "feat(mind_loop): truncate WAL after successful iterate"
```

---

## Task 4: Kernel WAL plumbing + startup replay

**Files:**
- Modify: `src/dollos/kernel.py`
- Extend: `tests/test_kernel.py`

- [ ] **Step 1: Failing test (or defer to E2E)**

```python
@pytest.mark.asyncio
async def test_kernel_replays_pending_wal_perceptions_on_startup(tmp_path):
    """Perceptions left in WAL from a previous run get pushed back into the queue."""
    # 1. Construct a WAL directly, append two Perceptions
    # 2. Construct DollOS with data.root = tmp_path
    # 3. After kernel.run() bootstraps, drain the perception queue
    # 4. Assert both perceptions came back
    # Note: this is tricky to write fully; OK to defer to Task 7 E2E smoke.
    pytest.skip("integration covered in Task 7 smoke")
```

- [ ] **Step 2: Implement**

In `src/dollos/kernel.py`:

1. Construct WAL at startup:
   ```python
   from dollos.wal.perception_log import PerceptionWAL
   ...
   wal_path = settings.data.root / "wal" / "perceptions.jsonl"
   self._wal = PerceptionWAL(wal_path)
   self._perception_queue = PerceptionQueue(wal=self._wal)
   ```

2. Before `mind_loop.run()` starts, replay pending WAL entries:
   ```python
   async def _replay_wal(self) -> None:
       """Push any pending WAL perceptions back into the queue before mind_loop starts."""
       pending = list(self._wal.iter_pending())
       if not pending:
           return
       logger.info("wal: replaying %d pending perceptions from previous run", len(pending))
       for seq, p in pending:
           # Note: p already has seq set; PerceptionQueue won't re-WAL it
           self._perception_queue.put(p)
   ```
   Call it in `run()` after WAL construction, before `mind_loop` starts.

3. Pass `wal` to MindLoop construction.

- [ ] **Step 3: Run tests**

```
uv run pytest --ignore=tests/voice -q
```

Green or only known pre-existing milvus failure.

- [ ] **Step 4: Commit**

```bash
git add src/dollos/kernel.py tests/test_kernel.py
git commit -m "feat(kernel): WAL plumbing + replay pending perceptions on startup"
```

---

## Task 5: PID lockfile + dirty restart detection

**Files:**
- Modify: `src/dollos/kernel.py`
- Create: `src/dollos/wal/pidfile.py` — small helper
- Create: `tests/test_pidfile.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_pidfile.py
import os
from pathlib import Path

from dollos.wal.pidfile import PidFile, RestartKind


def test_first_start_is_cold(tmp_path):
    pf = PidFile(tmp_path / "daemon.pid")
    kind = pf.acquire()
    assert kind == RestartKind.COLD
    assert pf.path.read_text() == str(os.getpid())


def test_clean_release_then_restart_is_cold(tmp_path):
    pf1 = PidFile(tmp_path / "daemon.pid")
    pf1.acquire()
    pf1.release()
    assert not pf1.path.exists()

    pf2 = PidFile(tmp_path / "daemon.pid")
    kind = pf2.acquire()
    assert kind == RestartKind.COLD


def test_dirty_restart_detected_when_pid_gone(tmp_path):
    """If the previous pid is no longer running and the file wasn't deleted, it's dirty."""
    path = tmp_path / "daemon.pid"
    path.write_text("99999")  # almost certainly not a running pid
    pf = PidFile(path)
    kind = pf.acquire()
    assert kind == RestartKind.DIRTY


def test_dirty_restart_replaces_pid(tmp_path):
    path = tmp_path / "daemon.pid"
    path.write_text("99999")
    pf = PidFile(path)
    pf.acquire()
    assert pf.path.read_text() == str(os.getpid())
```

- [ ] **Step 2: Implement**

```python
# src/dollos/wal/pidfile.py
"""PidFile — daemon-running marker for dirty restart detection."""
from __future__ import annotations

import enum
import os
from pathlib import Path


class RestartKind(enum.Enum):
    COLD = "cold"       # first start or clean previous shutdown
    DIRTY = "dirty"     # previous run crashed (pidfile left, pid no longer running)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


class PidFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def acquire(self) -> RestartKind:
        """Write our pid; return COLD or DIRTY based on previous state."""
        kind = RestartKind.COLD
        if self.path.exists():
            prev = self.path.read_text().strip()
            try:
                prev_pid = int(prev)
                if _pid_alive(prev_pid) and prev_pid != os.getpid():
                    # Another daemon instance is running — bail loud
                    raise RuntimeError(
                        f"another DollOS daemon appears to be running (pid={prev_pid})"
                    )
                if not _pid_alive(prev_pid):
                    kind = RestartKind.DIRTY
            except ValueError:
                kind = RestartKind.DIRTY  # corrupt file = treat as dirty
        self.path.write_text(str(os.getpid()))
        return kind

    def release(self) -> None:
        """Delete the pidfile on clean shutdown. Idempotent."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
```

- [ ] **Step 3: Pass**

- [ ] **Step 4: Wire into kernel**

In `kernel.py`:
- `__init__`: `self._pidfile = PidFile(settings.data.root / "daemon.pid")`
- `run()` start: `self._restart_kind = self._pidfile.acquire()`
- `run()` finally / shutdown: `self._pidfile.release()`

- [ ] **Step 5: Commit**

```bash
git add src/dollos/wal/pidfile.py tests/test_pidfile.py src/dollos/kernel.py
git commit -m "feat(wal): PidFile + dirty restart detection"
```

---

## Task 6: Recovered perception + scaffolding

**Files:**
- Modify: `src/dollos/kernel.py` — push `Awoke(reason="recovered")` on dirty restart
- Modify: `src/dollos/mind/mind_prompt.py` — render the recovered reason distinctly
- Modify: `src/dollos/prompts/templates/scaffolding.jinja` — optional short note about recovery
- Extend: `tests/test_mind_prompt.py`

- [ ] **Step 1: Failing tests**

```python
def test_awoke_recovered_renders_distinctly():
    import time
    from dollos.mind.mind_state import Perception
    from dollos.mind.mind_prompt import _percep_body

    p = Perception(kind="Awoke", t=time.time(), data={"reason": "recovered"})
    body = _percep_body(p)
    assert "recover" in body.lower() or "crash" in body.lower()


def test_awoke_cold_start_unchanged():
    import time
    from dollos.mind.mind_state import Perception
    from dollos.mind.mind_prompt import _percep_body

    p = Perception(kind="Awoke", t=time.time(), data={"reason": "cold_start"})
    body = _percep_body(p)
    assert "cold_start" in body  # current behavior preserved
```

- [ ] **Step 2: Implement**

In `kernel.py` `run()`, change the existing Awoke perception push to use `self._restart_kind`:
```python
if self._restart_kind == RestartKind.DIRTY:
    reason = "recovered"
elif self._mind_state.iter_count > 0:
    reason = "resumed"
else:
    reason = "cold_start"

self._perception_queue.put(
    Perception(kind="Awoke", t=time.time(), data={"reason": reason})
)
```

In `mind_prompt.py` `_percep_body`:
```python
if p.kind == "Awoke":
    reason = d.get("reason", "?")
    if reason == "recovered":
        return "the daemon just recovered from a crash — your previous in-flight thoughts may be partial or lost"
    return f"reason={reason}"
```

Scaffolding addition (optional, terse):
```jinja
# Recovery

If you see `Awoke(reason=recovered)`, the daemon was killed mid-thought
and just restarted. Some recent perceptions may have replayed; your
previous turn's output may have been cut off. Pick up naturally; don't
panic or explain the crash to the user unless they ask.
```

- [ ] **Step 3: Pass**

- [ ] **Step 4: Commit**

```bash
git add src/dollos/kernel.py src/dollos/mind/mind_prompt.py src/dollos/prompts/templates/scaffolding.jinja tests/test_mind_prompt.py
git commit -m "feat(recovery): Awoke(recovered) perception + scaffolding note"
```

---

## Task 7: E2E crash recovery smoke

**Files:**
- Create: `scripts/smoke_crash_recovery.py`

- [ ] **Step 1: Write smoke**

```python
"""smoke_crash_recovery.py — verify WAL replay + dirty restart detection.

Scenario:
  1. Start daemon. Send a TextInput. SIGKILL the daemon before processing completes.
  2. Restart daemon. Verify:
     - WAL replayed (the unprocessed UserSpoke came back into the queue)
     - Awoke(reason=recovered) perception fired
     - Doll responds in the restart (proves she processed the replayed perception)
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

# We need to launch the daemon as a separate process so we can SIGKILL it.
# Use a separate config file pointing at a tmp data dir.
```

Since SIGKILL'ing an in-process daemon from within Python is awkward, the smoke uses subprocess + signals. The full script is long; the subagent should write it out following the pattern of other smokes but adapted to subprocess control.

Key steps:
1. Write a minimal config TOML to tmp
2. Launch `uv run python -m dollos --config <tmp_config>` as a subprocess
3. Wait for IPC port to be ready
4. Connect WS, send `{"type": "text_input", "text": "你好, 第一次"}`
5. After 0.3s (short enough that Doll's likely still in iterate but unlikely to have completed save_state), SIGKILL the subprocess
6. Wait for it to die
7. Inspect `<tmp>/wal/perceptions.jsonl` — should have unprocessed entries
8. Inspect `<tmp>/daemon.pid` — should still exist (not cleanly removed)
9. Relaunch the daemon, connect WS, drain messages
10. Verify Doll responds (proving the replayed perception was processed)

- [ ] **Step 2: Pre-flight**

```bash
curl -s http://localhost:8001/health  # llama-server up
```

- [ ] **Step 3: Run**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/crash-recovery
uv run python -u scripts/smoke_crash_recovery.py 2>&1 | tee /tmp/smoke_crash.log
```

- [ ] **Step 4: Verify**

The smoke output should clearly show:
- WAL contained unprocessed entries after SIGKILL
- Second start replayed them
- Doll responded to the replayed input

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke_crash_recovery.py
git commit -m "test(recovery): crash recovery E2E smoke"
```

---

## Self-Review Checklist

**Spec coverage:**
- ✅ Perception WAL → Tasks 1, 2
- ✅ WAL truncate after iterate → Task 3
- ✅ Startup replay → Task 4
- ✅ PID lockfile dirty detection → Task 5
- ✅ Awoke(recovered) perception → Task 6
- ✅ Scaffolding recovery note → Task 6
- ✅ E2E smoke → Task 7

**Type consistency:**
- `PerceptionWAL`, `PidFile`, `RestartKind` consistent
- `Perception.seq: int | None` added — backwards compat for existing callers (default None)
- `PerceptionQueue.__init__` accepts optional `wal=`

**Out of scope, documented in plan header:**
- Active monitor revive (let Doll re-spawn)
- Subagent / Shell orphan handling
- Mid-LLM-stream recovery

**Known limitation:**
- WAL has no fsync — last few ms of perceptions may be lost on hard crash. Acceptable for personal daemon.
- WAL replay re-fires perceptions that may have been partially handled — e.g. NoteMemory written but Say not emitted. Doll might NoteMemory the same fact twice. Idempotency is the user's tolerance; rare in practice.

---

**Plan complete.**
