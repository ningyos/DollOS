# Persistent Mind Architecture Design

## Problem

DollOS's current dispatcher is a stateless reactive function:

- Each event triggers a fresh LLM cascade
- Each cascade reconstructs context from `ConversationHistory` + `Scratchpad` + `memsearch` retrieval
- Cascades end, state is discarded (except writes to memsearch / persistent files)
- Between events Doll "does not exist" as a process — no active thinking
- Multiple concurrent events spawn parallel cascades that don't see each other (the source of the IV-removal `ShellResult` regression)

This architecture cannot deliver three properties needed for a personal AI companion:

1. **Single consciousness** — Doll as one continuous mind, not a function called per-event
2. **Concurrent multi-task awareness** — multiple things happening simultaneously, all visible to current reasoning
3. **Global state cognition** — at any moment, knows current state of all ongoing concerns

## Solution: Persistent Mind

Replace the per-event dispatcher with ONE continuously-running coroutine (`MindLoop`) that holds a single mutable state object (`MindState`) and processes perceptions from a unified queue. Events become perceptions; the mind iterates continuously; idle periods fire idle ticks; state persists across iterations and daemon restarts.

Validated by prototype work at `experiments/persistent_mind/` (commits `1d56a1f`, `b85f5f8`):

- Cross-iteration commitment via `OpenLoop` / `CloseLoop` works without training
- `[Active tasks]` block surfaces multiple concurrent shells naturally — no special multi-task logic needed
- `recent_outputs` deque cures say-spam (mind doesn't repeat itself when she sees what she said recently)
- Memsearch integration: auto-RAG via `[Memory context]` block + explicit `Recall` + `NoteMemory`
- Cross-session persistence: daemon restart loads MindState, mind continues seamlessly
- Multi-task progress query test: mind correctly reports 3 parallel shells' statuses without confusion or hallucination

## Architecture

### One MindLoop coroutine

Daemon startup spawns ONE coroutine that runs forever:

```python
async def mind_loop():
    while not shutdown:
        # Drain perceptions; timeout fires idle tick
        perceptions = await drain_queue(timeout=idle_interval)
        state.recent_perceptions.extend(perceptions)
        
        # Auto-sync external state into MindState
        state.active_tasks = process_registry.snapshot()
        state.pending_events = schedule.upcoming()
        
        # Render full state as prompt
        prompt = render_mind(state)
        
        # ONE LLM call; output is 0..N actions
        actions = await llm.iterate(prompt)
        
        # Execute (sync inline OR fire-and-forget dispatch)
        for action in actions:
            await execute(action)
            state.recent_outputs.append(action_summary(action))
        
        state.iter_count += 1
        state.last_iter_at = now()
        state.persist()
```

Replaces the current `EventDispatcher` entirely.

### MindState (single source of truth)

```python
@dataclass
class MindState:
    # Always-on state (persists across daemon restart)
    mood: Mood                            # see Mood spec from existing step-19 work; unchanged
    focus: str                            # one sentence: what I'm currently attending to
    energy: float                         # 0-1, decays on idle, restored by perceptions
    scratchpad: str                       # Doll-mutable freetext (replaces existing Scratchpad)

    # Multi-concern (auto-synced from external sources per iteration)
    active_tasks: list[ActiveTask]        # from ProcessRegistry
    pending_events: list[PendingEvent]    # from Schedule
    open_loops: list[OpenLoop]            # Doll-explicit TODO commitments

    # Working memory (bounded deques)
    recent_perceptions: deque[Perception]
    recent_outputs: deque[OutputRecord]
    recent_thoughts: deque[Thought]

    # Continuity
    last_user_at: float
    last_iter_at: float
    iter_count: int
    session_started_at: float
```

#### Supporting types

```python
@dataclass
class ActiveTask:
    task_id: str                  # "shell-N" / "subagent-N" / "monitor-N"
    kind: Literal["shell", "subagent", "monitor"]
    summary: str                  # "ls /tmp" / task description / monitor command
    started_at: float
    @property
    def elapsed_s(self) -> float: ...

@dataclass
class PendingEvent:
    fire_at: float                # scheduled time (epoch)
    summary: str                  # "18:00 evening check-in"

@dataclass
class OpenLoop:
    id: str                       # short slug Doll picks
    desc: str
    opened_at: float

@dataclass
class Perception:
    kind: Literal["UserSpoke", "ToolResultArrived", "MonitorFired",
                  "MonitorEnded", "ScheduledMoment", "IdleTick", "Awoke"]
    t: float                      # arrival timestamp
    data: dict                    # kind-specific payload

@dataclass
class OutputRecord:
    t: float
    kind: str                     # action kind: "Say", "Dispatch", etc.
    summary: str                  # short rendered summary (≤80 chars)

@dataclass
class Thought:
    t: float
    text: str                     # raw Think action text or Recall result digest
```

Deque maxlens: `recent_perceptions=20`, `recent_outputs=15`, `recent_thoughts=10`. Tunable via config.

#### Mind state persistence

`MindState` serializes to `data/mind_state.json` via **atomic write** (write to `mind_state.json.tmp`, fsync, rename). On daemon start, load if present; on parse failure, log loudly and start fresh (no silent fallback to partial state).

Deques serialize as JSON arrays. `Mood` follows its existing serialization. Some fields refresh on load: `energy=1.0`, `session_started_at=now()`. Others keep disk value: `iter_count`, `mood`, `focus`, `scratchpad`, `open_loops`, `recent_perceptions/outputs/thoughts`.

`MindState` replaces `Scratchpad` (folded in) + `ConversationHistory` (replaced by `recent_perceptions` + `recent_outputs` + `recent_thoughts`).

### Perception types (unified input)

All event sources funnel into one `PerceptionQueue` (asyncio.Queue):

| Perception | Source | Replaces |
|---|---|---|
| `UserSpoke(text)` | WS text_input or ASR result | `UserTextEvent` |
| `ToolResultArrived(tool, result)` | Shell/Subagent/Monitor runners on completion | `ShellResultEvent` / `SubagentResultEvent` |
| `MonitorFired(monitor_id, line)` | MonitorRunner per matched line | `MonitorTriggeredEvent` |
| `MonitorEnded(monitor_id, exit_status)` | MonitorRunner on exit | `MonitorExitedEvent` |
| `ScheduledMoment(text)` | Schedule | `ScheduledEvent` |
| `IdleTick` | drain queue timeout | (new) |
| `Awoke(reason)` | daemon startup | (new — replaces implicit boot) |

### Action vocabulary (LLM output)

LLM outputs JSON array of 0..N actions per iteration. Multiple actions in one iteration is normal (e.g. `[Say, Dispatch, OpenLoop]`).

**Inline (sync, executed before next action)**:

| Action | Effect |
|---|---|
| `Say(text)` | Stream text to current sink (with TurnStart/TurnEnd brackets) |
| `Think(text)` | Append to `recent_thoughts`; not externalized |
| `SetFocus(text)` | Update `state.focus` |
| `SetMood(mood, reason?)` | Update `state.mood` |
| `OpenLoop(id, desc)` | Add to `state.open_loops` |
| `CloseLoop(id, outcome)` | Remove from `state.open_loops` |
| `WriteScratchpad(text)` / `AppendScratchpad(text)` / `EditScratchpad(old, new)` / `ClearScratchpad()` | Mutate `state.scratchpad` (same as existing) |
| `NoteMemory(text)` | Write to memsearch |
| `Recall(query)` | Search memsearch → append to `recent_thoughts` |
| `ReadToolOutput(id, offset, limit)` / `GrepToolOutput(id, pattern, max_matches)` | Read paged output |
| `InvokeSkill(name)` | Load skill body into next iteration's perception |
| `Idle` | Explicit no-op; skip emission |
| `Sleep(seconds)` | Extend next `idle_interval`; signals "nothing urgent for N seconds" |
| `RemoveMonitor(id)` | Stop a running monitor |

**Async dispatch (fire-and-forget, no return value)** — three distinct action types (separate to keep grammar simple and pydantic schemas tight):

| Action | Args | Effect |
|---|---|---|
| `Shell` | `command: str, timeout_s: int` | Spawn shell; on exit → `ToolResultArrived` perception |
| `SpawnSubagent` | `task: str, timeout_s: int` | Spawn subagent; on Report → `ToolResultArrived` |
| `SpawnMonitor` | `command: str, match_regex: str\|None, rate_limit_s: float` | Spawn monitor; per matched line → `MonitorFired`; on exit → `MonitorEnded` |

(Same names as current DollOS tools — easier migration; no new vocabulary for Doll to learn.)

**Empty action array** is valid output and equivalent to `[{"action": "Idle"}]` (no-op for the iteration). Prefer explicit `[{"action": "Idle"}]` for clarity in mind_log.

**Scratchpad action ABI**: `WriteScratchpad / AppendScratchpad / EditScratchpad / ClearScratchpad` action names and arg shapes are preserved from the existing `src/dollos/scratchpad.py`. Only the storage backend changes — instead of mutating a separate `Scratchpad` instance, they mutate `MindState.scratchpad`. Same 2000-char hard cap, same error semantics.

### Prompt structure (always-rendered)

```
SYSTEM
{character pack + mind manual + action vocabulary}
─── stable prefix (llama.cpp --cache-reuse hits here)

[Memory context]                    ← memsearch top-K per iteration
{bullets}

[Mind state]
focus: {focus}
mood: {mood}
energy: {energy}
session_age: {time since session_started_at}
last_user: {time since last_user_at}
iter: {iter_count}

[Active tasks]                      ← auto-synced from ProcessRegistry
- shell-1: command "ls /tmp", elapsed 3.2s
- subagent-2: task "search blender forums", elapsed 12s

[Open loops]
- find_blender_hair: opened 5min ago

[Pending]
- 18:00 evening check-in

[Scratchpad]
{state.scratchpad or "(empty)"}

[Recent perceptions] (newest last)
[12:01:23, 5s ago] UserSpoke: "幫我跑 ls /tmp"
[12:01:25, 3s ago] (I) Dispatched shell-1
[12:01:28, 0s ago] ToolResultArrived: shell-1, 18 lines, out-abc

[Recent outputs] (what you did recently — don't repeat yourself)
- [12:01:25, 3s ago] Dispatched Shell: ls /tmp
- [12:01:25, 3s ago] Said: 跑了，等一下
- [12:01:28, 0s ago] Said: 結果回來了，沒看到 dollos

[Recent thoughts]
- [12:01:23] User wants me to check /tmp. Dispatch shell.
- [12:01:28] Result back. No "dollos" string in output.

[Decision time]
What do you do this iteration? Output a JSON array of 0..N actions.
```

Block ordering chosen for two reasons:
1. **Cache-warmth gradient**: most stable content earlier (system → Mind state core → ... → recent_thoughts). llama.cpp `--cache-reuse` matches the longest common prefix; deeper into the prompt where content shifts every iter, less benefit but lower cost (later tokens cost the same as earlier).
2. **Cognitive flow for the LLM**: knowledge (`[Memory context]`) before self-state (`[Mind state]`) before situational concerns (active tasks / loops / pending) before recent history. Empirically, in the prototype the LLM consistently grounded answers in the last-rendered block ("recent outputs") and used the earlier blocks as background — so anti-repeat info goes near the bottom where attention focuses.

When `recent_perceptions` is empty (initial cold start before any event arrives), `[Memory context]` block renders `(no relevant memories — query was empty)`. Memsearch is queried with an empty string only on the first iteration; otherwise the most recent UserSpoke text (or last-3 perception summaries if no recent UserSpoke) is the query.

### Output to user (Say streaming)

`SinkResolver` is a daemon-level callable that returns the currently-active sink: the WS sink of the most-recently-connected client whose connection is still open. If no client connected, returns a dummy sink that drops messages (with a log line so we know Doll was talking to a wall).

When `Say(text)` action executes, the action handler calls `sink_resolver()` AT EMIT TIME, NOT at action-creation time. This means:
- If multiple Says fire across iterations, they each resolve fresh — surviving WS reconnects gracefully.
- If WS dropped between iterations, the new client (when it reconnects) starts fresh; the lost-in-disconnect Say is gone.
- No more per-cascade `response_sink` parameter threading through tools. Solves the IV-removal sink-sharing bug architecturally.

Each `Say` wraps its chunks in `TurnStart` / `TurnEnd` markers on the sink so the client knows utterance boundaries (multiple Says in one iteration = multiple TurnStart/TurnEnd pairs).

### Idle behavior + Sleep

Default `idle_interval = 10s`. On drain timeout (no perceptions for 10s), `IdleTick` perception fires.

`Sleep(N)` action extends next idle_interval to N seconds. The mind uses this to signal "nothing urgent for N seconds" — saves LLM compute.

Energy decays linearly over time when idle, restores on perception arrival. Mind can use energy as signal for "I'm dragging, maybe Sleep longer".

### Persistence

`MindState` serializes to `data/mind_state.json` after every iteration. Daemon startup loads the file → next iteration's `Awoke(reason="resumed")` perception lets mind orient. If file missing or corrupt, start fresh.

Deques serialize as lists. Some fields are ephemeral by intent (energy at 1.0 on restart; iter_count keeps counting from disk).

`memsearch` (long-term facts) persistence unchanged. ProcessRegistry / Schedule unchanged.

## Migration

### What dies

| Old | Reason |
|---|---|
| `EventDispatcher` (src/dollos/dispatcher.py) | Replaced by `MindLoop` |
| `ConversationHistory` (src/dollos/conversation_history.py) | Replaced by `recent_perceptions` + `recent_outputs` + `recent_thoughts` in MindState |
| `Scratchpad` (src/dollos/scratchpad.py) | Folded into MindState.scratchpad; tools stay (just point at MindState now) |
| Cascade compact / episodic auto-summary | Already removed in IV cleanup; not needed |
| Per-tool `response_sink` parameter | Sink looked up daemon-side per Say |

### What stays

| Component | Role unchanged |
|---|---|
| memsearch | Long-term RAG |
| ToolOutputStore | Shell/Subagent paged output |
| ShellRunner / SubagentRunner / MonitorRunner | Spawn processes; fire perceptions on completion |
| ProcessRegistry | Tracks running tasks; auto-syncs into MindState.active_tasks |
| Schedule | Time-based triggers; auto-syncs into MindState.pending_events |
| Tool pydantic schemas (Say, NoteMemory, Recall, Shell, etc.) | Same JSON shape; rebound to MindState |
| IPC server | WS in/out; UserSpoke perception in, Say chunks out |
| Voice pipeline | ASR chunks → UserSpoke perception; TTS sink unchanged |
| Character pack | system prompt source |
| cascade_log → renamed `mind_log` | Per-iter decision log |

### What changes shape

| Component | New shape |
|---|---|
| `Kernel` | Spawns one `MindLoop` coroutine on start; `await mind_loop_task` is the main waiting point |
| `ToolCtx` | Replaced by `MindCtx` containing `mind_state` (the live MindState reference), `memsearch`, `process_registry`, `tool_output_store`, `shell_runner` / `subagent_runner` / `monitor_runner`, `sink_resolver` (callable: () → sink), `memory_root` (for NoteMemory writes), `transcripts_root` |
| Tool `run()` methods | Take `MindCtx` instead of `ToolCtx`; mutate `mind_state` directly |
| Cascade logging | Per-iteration log (not per-cascade) |
| Tests | Per-iteration unit tests; new fixtures (`_make_mind`, `inject_perception`) |

## Engineering decisions (locked here so plan doesn't re-debate)

### Interrupt model: queue-and-iterate, not preempt

When `UserSpoke` arrives while MindLoop is mid-LLM-call:

1. Perception enters queue
2. Current iteration completes (LLM stream finishes naturally, ~1-3s)
3. Next iteration drains queue, sees the new perception
4. Mind reads `recent_perceptions` and sees "user interrupted me at 12:34"
5. Decides whether to abandon current focus and pivot

No mid-stream cancellation. Simpler, fully deterministic. Trade-off: up to 3s latency on user input. Acceptable for v1.

(If sub-second response becomes critical, add a preempt mode that cancels current `llm.iterate()` and starts fresh with the new perception. Not v1.)

### Multi-action in one iteration: required

LLM must output a JSON array (possibly empty). Single-action wrapping `[{...}]`. Multiple actions execute in order.

Prototype showed Qwen3.6 occasionally omitted outer `[]` brackets. Production must use GBNF grammar to lock the shape. New grammar rule `mind_actions := "[" action_call ("," action_call)* "]"` with strict outer brackets.

### Subagent: still single-cascade, no inner MindLoop

Subagent runs one task → Report → die. No multi-turn concept. Keeps current `SubagentRunner` design; result fires `ToolResultArrived` perception in parent Mind.

### Drone (future): own MindLoop per drone

Persistent agents (Drones) each get their own `MindLoop` + `MindState`. Multiple minds in one daemon. They communicate via perceptions (drone → Doll's queue, or drone → drone via daemon routing). Not in v1; Doll-only first.

### Memsearch query: derived per iteration

`[Memory context]` block content is `memsearch.search(query, top_k=10)` where `query` is derived from recent perceptions:

- If `recent_perceptions[-1]` is `UserSpoke`, use its text
- Otherwise concat the last 3 perceptions' summaries

Same per-iteration cost as current cascade-level retrieval. No small-LLM filter (per IV-removal decision).

### Voice pipeline: ASR chunks → batched UserSpoke

ASR completes utterance → produces final text → `UserSpoke(text)` perception. Partial chunks stay in voice pipeline buffer until utterance ends. No change to current ASR/TTS architecture; just the input side becomes a perception source.

### Daemon lifecycle

`Awoke(reason)` perception fires on startup with reason:
- `"cold_start"` (no persisted state)
- `"resumed"` (state loaded from disk, last_iter was N seconds ago)
- `"restart"` (graceful shutdown then restart)

Mind sees `Awoke` and can decide what to do (greet user, check pending tasks, summarize idle period).

`Sleep` actions persist via state — restart preserves "Doll was sleeping" info.

### `mind_log` (decision log)

Per-iteration JSONL log:

```jsonl
{"iter": N, "ts": ..., "perceptions": [...], "active_tasks": [...], "prompt_tokens": ..., "actions": [...], "latency_s": ...}
```

Replaces cascade_log. Smaller granularity (per-iter not per-cascade). Same disk location pattern: `data/mind_log/YYYY-MM-DD.jsonl`.

## Out of Scope

- Sub-second preempt interrupt (queue-and-iterate is v1)
- Multi-mind (Drone) — Doll-only first
- Streaming-LLM (Pattern A from survey) — llama.cpp can't
- Layer 2 rolling summary — recent_perceptions deque + memsearch covers it
- Migration from current state files to mind_state.json — fresh start acceptable for personal-use daemon

## Risks

1. **GPU cost**: continuous mind = continuous-ish LLM calls. ~10s idle_interval × 35B model × 1-3s per call = up to 8-9 calls/minute when idle. Mitigated by Sleep escalation but real. Acceptable for personal-use daemon; would need rework for SaaS.

2. **Prompt bloat**: long-run test showed prompt growing from 999 to ~5800 chars (5.8x). Deque maxlens bound it but real. Mitigated by tighter recent_perceptions trimming + periodic background summarization (drone-side later).

3. **Mind stuck in loop** (long-run found `SetFocus` repeat). Mitigated by `recent_outputs` block (Doll sees she set focus already). Re-validate post-refactor.

4. **JSON output without outer `[]`** (prototype caught). Production must enforce via GBNF grammar.

5. **State persistence corruption**: malformed mind_state.json on disk → fall back to cold start. Log loudly.

## Acceptance criteria

### v1 (day-1 ship)

- All four real-LLM e2e smokes (paging, scratchpad, conversation_history, multi-turn) behaviorally pass — same prompts, same expected outcomes
- New multi-task progress query test passes (3 parallel shells, mind reports progress accurately)
- New `IdleTick + Sleep` test: 5-minute idle run, no say-spam, mind Sleep-escalates
- New persistence test: kill daemon mid-cascade, restart, `Awoke(reason="resumed")` perception fires, MindState fields preserved
- Full pytest suite passes (existing tests adapted to MindLoop, no regressions)
- No port 8003 dependency (already removed; this is a regression guard)

### v1.x (post-ship, not blocking)

- Day-long (24h) stability: daemon survives extended idle + intermittent prompts, prompt size bounded, no runaway state growth
- mind_log analysis tool: read JSONL log to surface usage patterns (idle ratio, action distribution, latency percentiles)
