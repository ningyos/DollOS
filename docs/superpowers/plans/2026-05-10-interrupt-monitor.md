# Plan (Phase 3): Cancel + interrupt-aware Monitor

**Worktree**: `.worktrees/interrupt-monitor/`
**Branch**: `interrupt-monitor`
**Date**: 2026-05-10

## Why

Phase 3 of 4-phase plan. Phase 1 shipped pending awareness (Doll sees 
queued events between iters). Phase 2 shipped async Shell + Monitor 
(Monitor blocks until process done). Phase 3 connects them: Monitor 
**early-returns** when a pending event arrives during its wait, 
giving Doll natural interrupt handling without explicit Suspend/Resume 
state machines. Plus: `Cancel(handle)` tool to kill running processes.

After Phase 3, Doll's interrupt flow is:
1. Doll's cascade in iter 5, mid-Monitor on long Shell
2. UserTextEvent arrives during Monitor's wait → dispatcher pending
   queue + sets pending_signal
3. Monitor's `asyncio.wait` returns early — pending wins race
4. Monitor returns interrupt message; cascade iter ends
5. Next iter perception has `[Pending events]` block + interrupt msg
6. Doll naturally decides: Cancel? Continue Monitor? Wrap up Say?

No formal Suspend tool. Interrupt is implicit via Monitor's race 
behavior.

## Phase 3 scope

A. New `Cancel(handle)` tool — kill process, remove from registry
B. Monitor races `proc.communicate()` vs `pending_signal` — early-return 
   on interrupt
C. Dispatcher exposes `pending_signal: asyncio.Event` to ToolCtx
D. Scaffolding teaches: Monitor may early-return, here's how to react

## Out of scope

- Phase 4 (Subagent unify into same async/Monitor pattern)
- Cancellation of partial stdout state — interrupted Monitor leaves 
  process running with stdout consumed/partial; subsequent Monitor 
  on same handle MAY return empty or hang. Doll should Cancel + 
  respawn if she wants clean retry.
- Persistent task state across daemon restart
- Streaming output during Monitor (would let Doll see progress mid-
  process). Defer.

## Architecture

### Pending signal

Dispatcher state:
```python
self._pending_signal: asyncio.Event = asyncio.Event()
```

`dispatch()` flow change:
```python
def dispatch(self, raw: RawEvent) -> None:
    if isinstance(raw, SERIALIZE_TYPES):
        if self._active_cascade and not self._active_cascade.done():
            self._pending.append(raw)
            self._pending_signal.set()  # NEW: wakes Monitor
            return
        ...

def _on_cascade_done(self, _task) -> None:
    self._pending_signal.clear()  # NEW: reset for next cascade
    if self._pending:
        next_event = self._pending.pop(0)
        ...
```

`_on_cascade_done` clears signal because next cascade will see [Pending 
events] block on iter 1 and process accordingly.

ToolCtx gets `pending_signal: asyncio.Event | None`. Dispatcher passes 
it via `_respond`'s ToolCtx construction.

### Monitor early-return

```python
async def run(self, ctx: ToolCtx) -> str:
    managed = ctx.process_registry.get(self.handle)
    if managed is None:
        return f"unknown handle {self.handle!r}"
    
    proc = managed.proc
    proc_task = asyncio.create_task(proc.communicate())
    
    pending_signal = ctx.pending_signal
    waiters: list[asyncio.Task] = [proc_task]
    pending_task: asyncio.Task | None = None
    if pending_signal is not None:
        pending_task = asyncio.create_task(pending_signal.wait())
        waiters.append(pending_task)
    
    try:
        done, _ = await asyncio.wait(
            waiters,
            timeout=self.timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        proc_task.cancel()
        if pending_task is not None:
            pending_task.cancel()
        raise
    
    # Pending event won the race — early return
    if pending_task is not None and pending_task in done:
        proc_task.cancel()
        # Don't await proc_task — it'll be GC'd; process keeps running
        return (
            f"Monitor({self.handle!r}) interrupted by pending event. "
            f"Process still running. Use Cancel({self.handle!r}) to kill, "
            f"or skip and respond to pending."
        )
    
    # Process completed naturally
    if proc_task in done:
        if pending_task is not None:
            pending_task.cancel()
        try:
            stdout, _ = proc_task.result()
        except Exception as e:
            return f"Monitor({self.handle!r}) error reading output: {e}"
        output = stdout.decode("utf-8", errors="replace")
        ctx.process_registry.remove(self.handle)
        return _truncate(
            f"[exit {proc.returncode}]\n{output}",
            SHELL_OUTPUT_MAX_CHARS,
        )
    
    # Timeout (neither task in done)
    proc_task.cancel()
    if pending_task is not None:
        pending_task.cancel()
    return (
        f"Monitor({self.handle!r}) timed out after {self.timeout_s}s. "
        f"Process still running. Use Monitor again or Cancel."
    )
```

Note: cancelled `proc_task` doesn't kill the process; only cancels 
the read coroutine. Process keeps running. To kill it: `Cancel`.

### Cancel tool

```python
class Cancel(BaseModel):
    """Kill a background process started by Shell.
    
    Sends SIGKILL. Process must already be registered (via Shell).
    Use when Monitor was interrupted and you don't need the result, 
    or when a process times out and you want to stop it.
    """
    
    handle: str = Field(
        description="Handle returned by Shell (e.g., 'sh-1')."
    )
    
    async def run(self, ctx: ToolCtx) -> str:
        if ctx.process_registry is None:
            return "[Cancel unavailable: no process_registry in ToolCtx]"
        managed = ctx.process_registry.get(self.handle)
        if managed is None:
            return f"unknown handle {self.handle!r}"
        proc = managed.proc
        if proc.returncode is not None:
            ctx.process_registry.remove(self.handle)
            return f"process {self.handle!r} already exited with code {proc.returncode}"
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass  # process already gone
        except Exception as e:
            return f"failed to kill {self.handle!r}: {e}"
        ctx.process_registry.remove(self.handle)
        return f"killed {self.handle!r}"
```

Add to MAIN_TOOLS + SUB_TOOLS.

### ToolCtx extension

```python
@dataclass
class ToolCtx:
    sink: ...
    memory_root: ...
    memsearch: ...
    transcripts_root: ...
    subagent_runner: SubagentRunner | None = None
    process_registry: ProcessRegistry | None = None
    pending_signal: asyncio.Event | None = None  # new
```

### Dispatcher wiring

In `_respond`, when constructing ToolCtx per iter:
```python
ctx = ToolCtx(
    sink=sink,
    memory_root=...,
    ...,
    process_registry=self._process_registry,
    pending_signal=self._pending_signal,  # new
)
```

### Scaffolding update

`# Behavior`:
```
- Monitor 可能因 pending event early-return（returns 「interrupted by 
  pending event」訊息）。看到這訊息：
  - 想處理 pending 就 Say 收尾或回應，process 留著（用 Cancel 收）
  - 想繼續 Monitor 同 handle，再 emit Monitor (但部分 stdout 已消費，
    可能拿不到完整輸出，建議 Cancel + 重跑)
```

## Tests

`tests/test_tools.py`:
- `test_monitor_early_return_on_pending_signal`: build ProcessRegistry 
  + sleep process + asyncio.Event, set signal mid-Monitor, assert 
  Monitor returns interrupt message + process still running.
- `test_monitor_normal_completion_when_no_signal`: Monitor without 
  pending_signal (None), works as Phase 2.
- `test_monitor_normal_completion_when_signal_unset`: pending_signal 
  is Event but never set → Monitor completes normally.
- `test_cancel_kills_running_process`: register sleep proc, Cancel, 
  assert proc.returncode set + handle removed.
- `test_cancel_unknown_handle`: returns "unknown handle".
- `test_cancel_already_exited_process`: process exited, Cancel returns 
  "already exited with code".
- `test_cancel_no_registry`: ctx.process_registry None → 
  "[Cancel unavailable]".

`tests/test_dispatcher.py`:
- `test_dispatcher_pending_signal_set_when_serialize_event_queued`: 
  active cascade + dispatch UserTextEvent → assert pending_signal.is_set().
- `test_dispatcher_pending_signal_cleared_after_cascade_ends`: cascade 
  ends → pending_signal cleared.
- `test_dispatcher_provides_pending_signal_to_tool_ctx`: capture ctx, 
  assert pending_signal is not None.

`tests/test_kernel.py`:
- No new tests; pending_signal is dispatcher-internal.

`tests/test_subagent.py`:
- `test_subagent_ctx_has_pending_signal_or_none`: subagent's ToolCtx 
  has pending_signal attribute (None acceptable for sub-cascade since 
  subagent has its own loop).

## Risks

- **Process leak via cancelled communicate()**: `proc_task.cancel()` 
  cancels the read coroutine but leaves process. If Doll doesn't 
  Cancel, process runs until daemon shutdown (registry shutdown kills 
  all). Acceptable.
- **Re-Monitor after early-return**: communicate() partially consumed 
  stdout pipe. Re-calling communicate() on same proc may hang or 
  return empty. Document and recommend Cancel + respawn.
- **Pending signal racy**: dispatcher may set pending_signal twice 
  (two events queued). Monitor races same way. Once set, repeat sets 
  are no-op. Once cleared by `_on_cascade_done`, next cascade starts 
  clean.
- **Subagent inherits pending_signal**: if main passes its 
  pending_signal to SubagentRunner, sub-cascade's Monitor would 
  early-return when MAIN cascade has pending. Probably wrong. 
  Subagents should have their OWN pending_signal (or None — they 
  don't have a "queue" to interrupt for). For Phase 3 MVP: subagent 
  ToolCtx gets pending_signal=None.

## Acceptance

- [ ] `uv run pytest` 全綠.
- [ ] Smoke (T1-T8 + manual interrupt scenario):
  - Normal T4/T5 (Shell + Monitor) work as Phase 2.
  - Manual: send T_long with sleep 30s Shell → during Monitor, send 
    UserText → Monitor early-returns with interrupt message → Doll 
    handles new UserText.
  - Cancel kills processes cleanly.
- [ ] No regression on T1-T8 base.
