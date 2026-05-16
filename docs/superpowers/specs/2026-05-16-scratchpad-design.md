# Scratchpad Design

## Problem

DollOS's external actions (Shell, SpawnSubagent, SpawnMonitor) are fire-and-forget — they dispatch and return immediately, and the result re-enters the event loop as a new perception in a future turn. The originating turn's intent does not follow the result back.

Observed concretely (2026-05-16 real-LLM e2e smoke for tool-output paging): user asks Doll to "run `seq 1 200` and tell me what line 150 is". Doll:

1. T1, iter 0: calls `Shell("seq 1 200")` ✓
2. T1, iter 1: says "I'll tell you when it's back"
3. T2 (ShellResultEvent): sees `output_id=out-9249fc46`, 200 lines — but the perception body has no record of *why* she ran the shell. Doll asks the user what they wanted instead of completing the original task.

The new `ReadToolOutput` / `GrepToolOutput` tools from the tool-output-paging plan are correctly wired and reach Doll's perception, but she does not use them because the originating intent is gone.

This is an architectural gap. The fix is to give Doll a small writable working-memory block that persists across turns within a daemon session.

## Solution: Scratchpad

A 2000-character ephemeral text document, owned by the daemon process, auto-rendered at the top of every Doll perception, mutated by Doll via four pydantic tools. Doll writes her open goal / current task before firing an external action; when the result arrives in a new turn, she sees the scratchpad and remembers what she was doing.

Inspired by:
- **MemGPT / Letta "core memory"** — in-context editable block, agent-mutated via tool calls.
- **Claude Code Edit semantics** — `(old_string, new_string)` unique-match replacement, familiar to LLMs from training data.
- **Manus "current task" buffer** — observation-carrying state.

## Properties

| Property | Value | Why |
|---|---|---|
| Persistence | Ephemeral (process lifetime) | Working memory, not long-term. Long-term lives in memsearch via `NoteMemory`. |
| Size cap | 2000 chars, hard reject on overflow | Bounded context cost; forces discipline on what belongs here vs in memory. |
| Visibility | Auto-rendered in every Doll perception | Required to actually solve the T2-forgetting problem. |
| Backing | In-memory string (no file) | Ephemeral by design; no IO overhead. |
| Namespace | Single per daemon process | DollOS runs one character at a time. Multi-character can add namespacing later. |
| Empty state | Rendered as `(empty)` | Doll sees the block exists even when unused. |
| Initial content | Empty at daemon startup | No bootstrapping. |
| Lifecycle | Doll-managed via `ClearScratchpad()` | She is taught (scaffolding) to clear when current task is done. No auto-clear. |

## Tools

Four pydantic `BaseModel` subclasses registered in both `MAIN_TOOLS` and `SUB_TOOLS`. Subagents get scratchpad access too — they may need to track multi-step task state internally just like Doll.

### `WriteScratchpad`

```python
class WriteScratchpad(BaseModel):
    """Overwrite the scratchpad with new content.

    Hard cap 2000 chars. Use this when starting fresh or when the
    existing content is irrelevant.
    """
    content: str = Field(...)

    async def run(self, ctx) -> str:
        ctx.scratchpad.write(self.content)
        return f"scratchpad set ({len(self.content)} chars)"
```

### `AppendScratchpad`

```python
class AppendScratchpad(BaseModel):
    """Append a line to the end of the scratchpad.

    A newline is auto-prepended if the scratchpad is non-empty.
    Raises ValueError if appending would exceed 2000 chars.
    """
    text: str = Field(...)

    async def run(self, ctx) -> str:
        new_total = ctx.scratchpad.append(self.text)
        return f"scratchpad now {new_total} chars"
```

### `EditScratchpad`

```python
class EditScratchpad(BaseModel):
    """Replace a unique substring in the scratchpad.

    Same semantics as Claude Code's Edit: `old_string` must appear
    exactly once in the current contents. Use longer `old_string`
    with surrounding context if a short substring is ambiguous.
    Raises ValueError on no-match, ambiguous-match, or if the result
    would exceed 2000 chars.
    """
    old_string: str
    new_string: str

    async def run(self, ctx) -> str:
        ctx.scratchpad.edit(self.old_string, self.new_string)
        return "scratchpad edited"
```

### `ClearScratchpad`

```python
class ClearScratchpad(BaseModel):
    """Wipe the scratchpad to empty."""

    async def run(self, ctx) -> str:
        ctx.scratchpad.clear()
        return "scratchpad cleared"
```

## Scratchpad Class

```python
# src/dollos/scratchpad.py
class Scratchpad:
    """In-memory working memory for Doll. Lifecycle-bound to the daemon."""

    HARD_CAP = 2000

    def __init__(self) -> None:
        self._content = ""
        # No lock: tool calls within a cascade iter are awaited sequentially.
        # If true concurrent mutation becomes a real scenario, add asyncio.Lock.

    def read(self) -> str:
        return self._content

    def write(self, content: str) -> None:
        if len(content) > self.HARD_CAP:
            raise ValueError(
                f"scratchpad write exceeds {self.HARD_CAP} char cap "
                f"({len(content)} chars). Edit or Clear first."
            )
        self._content = content

    def append(self, text: str) -> int:
        sep = "\n" if self._content else ""
        new_total = len(self._content) + len(sep) + len(text)
        if new_total > self.HARD_CAP:
            raise ValueError(
                f"scratchpad append would exceed {self.HARD_CAP} chars "
                f"({new_total} after append). Edit or Clear first."
            )
        self._content = self._content + sep + text
        return new_total

    def edit(self, old: str, new: str) -> None:
        count = self._content.count(old)
        if count == 0:
            raise ValueError(f"old_string not found in scratchpad: {old!r}")
        if count > 1:
            raise ValueError(
                f"old_string appears {count} times — add more context to disambiguate"
            )
        new_content = self._content.replace(old, new, 1)
        if len(new_content) > self.HARD_CAP:
            raise ValueError(
                f"edit would push scratchpad to {len(new_content)} chars "
                f"(cap {self.HARD_CAP})."
            )
        self._content = new_content

    def clear(self) -> None:
        self._content = ""
```

## Perception Rendering

The `[Scratchpad]` block goes first in the perception body — it is the most immediate context (Doll's own working memory).

Order in the user message:

```
[Scratchpad]
<contents or "(empty)">

[Memory context]
...

[Now]
2026-05-16 ...

[Active monitors]
...

[Pending events]
...

[Message]
<actual perception>
```

Render logic in `dispatcher.py`'s `_build_perception_blocks` (or equivalent block assembly): always include `[Scratchpad]` block. Body is `ctx.scratchpad.read()` if non-empty, else `(empty)`.

## Architecture Integration

- New file: `src/dollos/scratchpad.py` — `Scratchpad` class + the four pydantic tool classes.
- `ToolCtx` gets a new required field: `scratchpad: Scratchpad`.
- `EventDispatcher.__init__` gets a new required kwarg `scratchpad`.
- `SubagentRunner.__init__` likewise — subagents have their own `Scratchpad` instance (do not share with main Doll, since their task is separate).
- `Kernel` creates `self._scratchpad = Scratchpad()` at init, passes to dispatcher. Subagent runner creates fresh `Scratchpad` per spawned subagent.
- `Kernel` shutdown: nothing to clean up (in-memory state vanishes with the process).
- Pre-existing `ToolCtx` / `EventDispatcher` / `SubagentRunner` callsites in tests need updating (the same pattern as the tool-output-paging plan).

## Scaffolding Update

Add to `scaffolding.jinja` (Behavior section):

> Scratchpad is your working memory — a 2000-char notepad that persists across turns within this session. Use `WriteScratchpad(content="...")` to set the full contents, `AppendScratchpad(text="...")` to add a line, `EditScratchpad(old_string, new_string)` to replace a unique substring, `ClearScratchpad()` to wipe. **Before firing a Shell or Subagent, write your current goal so you remember it when the result comes back as a new turn.** Clear the scratchpad when the task is done — stale notes confuse you.
>
> Suggested structure (markdown convention, not enforced):
>
> ```
> # Current goal
> what I'm trying to do
>
> # TODO
> - [ ] step 1
> - [x] step 2 (done)
> ```

Add to `subagent_scaffolding.jinja` (Behavior section):

> Scratchpad is your own private working memory — a 2000-char notepad scoped to this subagent run. Doll's parent scratchpad isn't visible here; yours starts empty and disappears when your task ends. Use `WriteScratchpad / AppendScratchpad / EditScratchpad / ClearScratchpad` to track multi-step state across tool results within your task.

## Testing

### Unit (`tests/test_scratchpad.py`)

- `write` happy path
- `write` exceeds cap → ValueError
- `append` happy path (empty → text)
- `append` happy path (existing → existing\nnew)
- `append` would exceed cap → ValueError
- `edit` unique-match happy path
- `edit` no-match → ValueError
- `edit` ambiguous-match → ValueError
- `edit` resulting overflow → ValueError
- `clear` happy path
- `clear` then write — round trip

### Tool tests (`tests/test_tools.py` additions)

For each of `WriteScratchpad` / `AppendScratchpad` / `EditScratchpad` / `ClearScratchpad`:
- `run()` happy path returns descriptive string and mutates ctx.scratchpad
- `run()` propagates ValueError from the store

### Dispatcher integration (`tests/test_dispatcher_*` or new file)

- Perception rendering includes `[Scratchpad]\n(empty)\n\n` block when scratchpad is empty
- Perception rendering includes `[Scratchpad]\n<contents>\n\n` block when non-empty
- `[Scratchpad]` block precedes `[Memory context]`

### Kernel wiring (`tests/test_kernel.py`)

- `dollos._scratchpad` is a `Scratchpad` instance after construction
- Scratchpad state mutates are observable via subsequent perception rendering

### E2E (`scripts/smoke_doll_scratchpad_e2e.py`)

Follow-up to `smoke_doll_paging_e2e.py`. Same prompt: "run `seq 1 200` and tell me line 150". Expected:

- T1 iter 0: Doll calls `WriteScratchpad("...find line 150 from seq 200 output...")` and `Shell(seq 1 200)`.
- T2 (ShellResultEvent): perception still shows the scratchpad. Doll reads it, sees `output_id` in the same perception, and calls `ReadToolOutput(id=..., offset=149, limit=1)`.
- T3 (ReadToolOutputResult): Doll says "line 150 is 150" and calls `ClearScratchpad()`.

Pass criterion: Doll calls `ReadToolOutput` with `offset` ≈ 149 AND her final Say explicitly identifies "150" as the value of line 150 (not as a number elsewhere). Observational, not a pytest assertion — log the cascade trace and judge by eye.

## Out of Scope

- Persistence across daemon restarts (deferred; current design is ephemeral by intent).
- Structured TODO sub-system (Doll handles TODO via markdown convention in free text).
- Per-character namespacing (DollOS runs one character per process today).
- Section-aware operations (e.g. "edit only the TODO section"). Free text + Edit is enough.
- Scratchpad history / undo. Last write wins.
- Subagent-to-Doll scratchpad inheritance. Subagents start with their own empty scratchpad.

## Rollout

Single plan, no phasing needed. The change is mechanical: new file, four tools, dispatcher block, kernel wiring, scaffolding edit, tests. Similar size and shape to the tool-output-paging plan.
