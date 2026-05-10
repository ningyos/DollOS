# Plan (Phase 2): Async Shell + Monitor tool + ProcessRegistry

**Worktree**: `.worktrees/async-shell/`
**Branch**: `async-shell`
**Date**: 2026-05-10

## Why

Phase 2 of 4-phase plan. Phase 1 shipped schedule + pending awareness 
(events queue but no formal Suspend mechanism). Phase 2 introduces 
the **async tool pattern** that will eventually make interrupt 
natural: tool calls return handles immediately, Doll explicitly 
Monitors them. This separates "starting work" from "waiting for 
result", giving Doll cooperative checkpoints.

Phase 2 specifically converts Shell to async + adds Monitor, but 
DOES NOT yet have interrupt early-return (Phase 3). Monitor blocks 
until completion or timeout.

## Phase 2 scope

A. `ProcessRegistry` — kernel-level process tracker, hands out handles
B. `Shell` becomes async — return handle immediately, don't block
C. `Monitor(handle, timeout_s)` — block cascade iter until process done
D. ToolCtx extended with `process_registry`
E. Scaffolding teaches Doll the async-Shell + Monitor pattern

## Out of scope

- `Cancel(handle)` tool — Phase 3
- Interrupt early-return on pending events — Phase 3
- Subagent unify into same pattern — Phase 4
- Other tools async (Recall, NoteMemory, etc) — keep sync
- Persistent registry across daemon restart — registry is in-memory

## Trade-off

For short Shell (pwd, ls), async pattern adds an iter:
- Sync: 1 iter (Shell + result inline)
- Async: 2 iters (Shell → handle, Monitor → output)

Cost: extra big-model call (~3-5s).

For long Shell (network, build), async unblocks cascade:
- Sync: cascade frozen 30s
- Async: handle in 1 iter, Monitor in next iter, model can reason about timeout / kill / etc

Phase 3 will let Monitor early-return on interrupt, making async truly 
worth it. Phase 2 alone is ergonomically slightly worse for short 
tasks; documentation should suggest Doll batch tool calls per iter 
when she knows tasks are short.

## Architecture

### `src/dollos/process_registry.py` (new)

```python
import asyncio
from dataclasses import dataclass, field


@dataclass
class ManagedProcess:
    handle: str
    proc: asyncio.subprocess.Process
    command: str  # for debug


class ProcessRegistry:
    """Tracks running subprocesses for async Shell + Monitor.
    
    Handles look like 'sh-1', 'sh-2', ... (incrementing). Removed 
    after Monitor consumes. Stale handles (timeout / never monitored) 
    cleaned up by Cancel tool (Phase 3) or daemon shutdown.
    """
    
    def __init__(self) -> None:
        self._processes: dict[str, ManagedProcess] = {}
        self._counter = 0
    
    def register(
        self, proc: asyncio.subprocess.Process, command: str
    ) -> str:
        self._counter += 1
        handle = f"sh-{self._counter}"
        self._processes[handle] = ManagedProcess(
            handle=handle, proc=proc, command=command
        )
        return handle
    
    def get(self, handle: str) -> ManagedProcess | None:
        return self._processes.get(handle)
    
    def remove(self, handle: str) -> None:
        self._processes.pop(handle, None)
    
    async def shutdown(self) -> None:
        """Kill any still-running processes on daemon shutdown."""
        for mp in list(self._processes.values()):
            if mp.proc.returncode is None:
                try:
                    mp.proc.kill()
                    await mp.proc.wait()
                except Exception:
                    pass
        self._processes.clear()
```

### `src/dollos/tools.py` — Shell async + new Monitor

Replace Shell:

```python
class Shell(BaseModel):
    """Spawn a shell command in the background. Returns a handle 
    immediately. Use Monitor(handle) to wait for output.
    
    Subprocess runs with the daemon's user permissions, working 
    directory at settings.data.root. Each Shell invocation is a 
    fresh subprocess.
    """
    
    command: str = Field(
        description="The shell command to run (will be passed to bash -c)."
    )
    
    async def run(self, ctx: ToolCtx) -> str:
        cwd = ctx.memory_root.parent
        proc = await asyncio.create_subprocess_shell(
            self.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
        )
        handle = ctx.process_registry.register(proc, self.command)
        return (
            f"shell {handle} dispatched (command={self.command!r}). "
            f"Use Monitor({handle!r}) to wait."
        )
```

Add Monitor:

```python
class Monitor(BaseModel):
    """Wait for a background process (from Shell) to finish.
    
    Blocks until the process exits or timeout. Returns combined 
    stdout+stderr with exit code. Removes the handle from the registry.
    """
    
    handle: str = Field(
        description="Handle returned by Shell (e.g., 'sh-1')."
    )
    timeout_s: int = Field(
        default=60,
        ge=1,
        le=600,
        description="Wait at most this many seconds. Default 60.",
    )
    
    async def run(self, ctx: ToolCtx) -> str:
        managed = ctx.process_registry.get(self.handle)
        if managed is None:
            return f"unknown handle {self.handle!r} (already monitored or not found)"
        
        proc = managed.proc
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.timeout_s,
            )
            output = stdout.decode("utf-8", errors="replace")
            ctx.process_registry.remove(self.handle)
            return _truncate(
                f"[exit {proc.returncode}]\n{output}",
                SHELL_OUTPUT_MAX_CHARS,
            )
        except asyncio.TimeoutError:
            return (
                f"Monitor({self.handle!r}) timed out after {self.timeout_s}s. "
                f"Process still running. Use Cancel({self.handle!r}) to kill, "
                f"or Monitor again to keep waiting."
            )
```

Both in `MAIN_TOOLS`. Both in SUB_TOOLS too — subagent can use.

`_truncate` already exists in tools.py (used by old Shell).

### `ToolCtx` extension

```python
@dataclass
class ToolCtx:
    sink: asyncio.Queue[ServerMessage | None]
    memory_root: Path
    memsearch: "MemSearch"
    transcripts_root: Path
    subagent_runner: SubagentRunner | None = None
    process_registry: ProcessRegistry | None = None  # new
```

### Kernel wiring

`__init__`:
```python
from dollos.process_registry import ProcessRegistry
self.process_registry = ProcessRegistry()
```

Pass to dispatcher (which builds ToolCtx):
- Update dispatcher's `_respond` ToolCtx construction to include 
  `process_registry=self._process_registry`.

Shutdown:
- Add `await self.process_registry.shutdown()` to daemon shutdown 
  sequence.

### SubagentRunner inheritance

SubagentRunner builds its own `SubagentToolCtx` with subset of fields. 
Add `process_registry` to that too so subagent can also use Shell + 
Monitor.

### Scaffolding update

`# Tools` description — Shell and Monitor's pydantic docstrings 
already explain. Plus `# Behavior` could note:

```
- Shell 是 async：返回 handle，**不 block**。要看結果用 Monitor(handle) 等。
  - 短任務（pwd, ls）：可以連續 emit Shell + Monitor in one cascade
  - 長任務（網路、build）：emit Shell 後可以做別的事，再回來 Monitor
```

Add to scaffolding `# Behavior` bullets.

## Tests

`tests/test_process_registry.py` (new):
- `test_register_returns_unique_handles`
- `test_get_returns_managed_process`
- `test_remove_drops_handle`
- `test_shutdown_kills_running_processes`

`tests/test_tools.py`:
- `test_shell_returns_handle_not_output`: Shell.run with `echo hello`, 
  assert returned str matches `shell sh-N dispatched ...`. Process is 
  registered.
- `test_monitor_waits_for_process`: register a quick process, call 
  Monitor.run, assert returns `[exit 0]\nhello\n` or similar.
- `test_monitor_unknown_handle`: call Monitor with bad handle, returns 
  `unknown handle` str.
- `test_monitor_timeout`: register a `sleep 5` process, Monitor with 
  timeout_s=1, returns timeout message.
- `test_shell_command_failure_in_monitor`: Shell with `false` → Monitor 
  returns `[exit 1]`.

`tests/test_dispatcher.py`:
- `test_dispatcher_provides_process_registry_to_tool_ctx`: dispatch 
  cascade, capture ctx, assert `ctx.process_registry` is the kernel's 
  registry.

`tests/test_kernel.py`:
- `test_kernel_creates_process_registry`: kernel has `process_registry` 
  attribute.
- `test_kernel_shutdown_cleans_processes`: after kernel shutdown, 
  registry's processes killed.

`tests/test_subagent.py`:
- `test_subagent_ctx_has_process_registry`: subagent's ctx also has it.

## Risks

- **Process leak if Doll never Monitors**: Doll could Shell and never 
  follow up. Process keeps running until registry shutdown. Phase 3 
  Cancel tool + scaffolding hint mitigate.
- **Stdout buffering**: Monitor reads ALL stdout via `communicate()`. 
  Long-running process with growing output OK because it's only read 
  on Monitor call. But process could fill OS pipe buffer (4KB default) 
  and block. For Phase 2 acceptable; Phase 3 streaming Monitor would 
  fix.
- **Async Shell + serialize cascade**: cascade still serialize (Phase 
  1). If Shell takes 30s, dispatcher can't process other events 
  during the cascade EVEN THOUGH Shell itself isn't blocking the 
  cascade — the cascade is blocked waiting for big-model. Phase 3 
  interrupt-aware Monitor solves.
- **Subagent + Process registry sharing**: subagent's Shell uses same 
  registry as main. If main spawns Shell handle "sh-1", subagent 
  could potentially Monitor it (or even Cancel later). Single global 
  registry is intentional per spec event-driven model. Acceptable.

## Acceptance

- [ ] `uv run pytest` 全綠.
- [ ] Smoke (T1-T8 with new Shell behavior):
  - T4「用 Shell 跑 pwd」: Doll either (a) cascade-spans Shell + Monitor 
    in 2 iters and reports pwd output, or (b) reports the dispatch 
    message (no Monitor) — depends on model.
  - T5「用 Shell ls data」: similar pattern.
  - Other turns unchanged behavior.
- [ ] No regression on T1/T2/T3/T6/T7/T8.
