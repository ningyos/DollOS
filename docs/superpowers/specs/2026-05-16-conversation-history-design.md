# Conversation History (Sliding Window) Design

## Problem

DollOS turns are independent cascades: when a `ShellResultEvent` arrives in T2, Doll's LLM call gets a fresh message list (`[system, user_msg_T2]`). The conversation that produced the original Shell call in T1 — including her own `<think>` block where she stated her goal, her tool call, and her Say to the user — is GONE from the LLM's view.

Real-LLM e2e smokes confirmed this hurts behavior:

- **paging smoke (pre-scratchpad)**: T2 ignored the original "find line 150" goal; Doll just acknowledged the shell finished.
- **scratchpad smoke**: even with `[Scratchpad]` block carrying her goal across turns, T2 still wrote "I didn't explicitly call seq 1 200 in a previous turn visible here" and disconnected the incoming result from her own action. Scratchpad helps but isn't sufficient — the missing context is the reasoning trace, not just the goal text.

The fix is a conversation history sliding window: at the start of each new turn, prepend the last N turns' full message lists (user + assistant + tool messages) to what gets sent to the LLM.

## Solution: ConversationHistory

A bounded per-DollOS-process buffer of recent turn transcripts. After each turn cascade ends, the dispatcher snapshots that turn's full message list into the history. When a new turn starts, history.recent_turns(N) is flattened and prepended to the LLM messages.

Inspired by:
- **LangChain ConversationBufferWindowMemory** — last K turns verbatim.
- **Industry consensus from prior survey** — K = 6-8 is the standard tail buffer.

## Properties

| Property | Value | Why |
|---|---|---|
| Storage | In-memory list of turn-message-lists | Ephemeral; matches scratchpad lifecycle. |
| Bound | Turn-count, default `K = 6` | Token-count would require tokenizer awareness; turn-count is simpler and "6 turns" is industry standard. |
| Persistence | Process lifetime | Same as scratchpad. |
| Scope | One per DollOS daemon | Per-character via daemon-per-character. |
| Subagent | Not wired — subagents are single-cascade (one task → Report → done); no cross-turn boundaries to bridge | The cascade's own accumulating messages list already provides full context within that single run. |
| Storage granularity | Full message list per turn (system message excluded; that's re-rendered fresh) | LLM sees complete reasoning trace, not just user+final-Say. |
| Configurability | `K` exposed via DollOS config | User can tune for context budget. |

## Why turn-count not token-count

Survey found K=6-8 is the practical standard, even though turns vary in size. Token-counting adds tokenizer dependency + early-eviction edge cases without proven quality benefit. If individual turns blow up (huge cascade iterating 30 times), token-count of a single retained turn could still dominate context — but that's a separate problem about cascade length, not about history.

## What constitutes a "turn"

A turn = one cascade triggered by a single `RawEvent` (UserTextEvent / ShellResultEvent / SubagentResultEvent / ScheduledEvent / etc.).

Per cascade, the LLM message list grows like:
```
[system, user_msg_iter0,
 assistant_iter0 (with think + tool_call),
 tool_response_iter0,
 user_iter1_dynamic_blocks_maybe,
 assistant_iter1, ...]
```

The full list (minus system) is what gets stored as one turn's transcript.

Within-cascade iters do NOT trigger storage; only the cascade END triggers `history.add_turn(transcript)`.

## Edge cases

- **Cascade crashes mid-turn**: store what we have anyway (the partial messages still inform what Doll was doing). The error message at the end is part of the trace and useful for the next turn.
- **First turn ever**: history is empty; new turn sees `[system, user_msg_T1]` (current behavior).
- **Empty turns**: shouldn't happen, but if a cascade ends with zero messages, skip storing.
- **Tool results with huge output_id-tagged previews**: already capped by paging; per-turn size bounded.

## Architecture Integration

### New file: `src/dollos/conversation_history.py`

```python
class ConversationHistory:
    """In-memory bounded window of recent turn transcripts.

    Each "turn" is the full LLM message list (excluding the system message)
    from one cascade. New turns are prepended to the LLM message list
    before send so the model sees recent reasoning.
    """

    def __init__(self, max_turns: int = 6) -> None:
        self._max_turns = max_turns
        self._turns: list[list[dict]] = []

    def add_turn(self, messages: list[dict]) -> None:
        """Append a turn's full message list to history; drop oldest if over cap."""
        if not messages:
            return
        self._turns.append(list(messages))   # defensive copy
        if len(self._turns) > self._max_turns:
            self._turns = self._turns[-self._max_turns:]

    def recent_messages(self) -> list[dict]:
        """Flatten retained turns into a single message list (system excluded)."""
        out: list[dict] = []
        for turn in self._turns:
            out.extend(turn)
        return out

    def turn_count(self) -> int:
        return len(self._turns)

    def clear(self) -> None:
        self._turns.clear()
```

### Config: `src/dollos/config.py`

Add to `Settings` (under a new `ConversationHistoryConfig` section):

```python
class ConversationHistoryConfig(BaseModel):
    max_turns: int = Field(6, ge=1, le=50, description="conversation window size in turns")


class Settings(BaseModel):
    ...existing fields...
    conversation_history: ConversationHistoryConfig = Field(default_factory=ConversationHistoryConfig)
```

And expose in `config.example.toml`:

```toml
[conversation_history]
max_turns = 6   # last N turns of dialogue kept in LLM context
```

### Wiring

- `Kernel` instantiates `self._conversation_history = ConversationHistory(max_turns=settings.conversation_history.max_turns)` at startup.
- `EventDispatcher.__init__` gains required `conversation_history: ConversationHistory` kwarg.
- At cascade start (`_respond` or equivalent), dispatcher reads `history.recent_messages()` and prepends them between system prompt and the new user message:
  ```python
  messages = [
      {"role": "system", "content": system_prompt},
      *self._conversation_history.recent_messages(),
      {"role": "user", "content": first_user},
  ]
  ```
- At cascade END (after the inner loop terminates), dispatcher calls `self._conversation_history.add_turn(messages[1:])` — store everything except the (fresh-per-turn) system message.
- `SubagentRunner` is NOT wired with history. Subagent cascades are single-turn (no Report → no end → no new cascade), so cross-turn history doesn't apply. The subagent's own accumulating messages list within `_run_cascade` provides full context for that one run.

### ToolCtx

`ToolCtx` does NOT get a `conversation_history` field — history is dispatcher-managed, not tool-accessible. Doll doesn't need a `ClearHistory` tool (clearing would corrupt context); the buffer is purely automatic.

### Tests

Unit (`tests/test_conversation_history.py`):
- Empty initial state
- `add_turn` then `recent_messages` round-trip
- Cap eviction: add K+1 turns, oldest dropped
- Empty messages list ignored
- `clear` resets

Integration:
- `tests/test_dispatcher_*`: assert that on turn N+1, the LLM call's message list contains turn N's messages between system and new user message.
- `tests/test_kernel.py`: assert `dollos._conversation_history` exists with configured `max_turns`.

### E2E

A third smoke building on the prior two: same "find line 150" prompt. Expected behavior with conversation history in place:

- T1: Doll's full cascade visible. She writes scratchpad + Shell + Say.
- T2: LLM message list now includes T1's user msg + Doll's assistant reasoning + Say. Doll sees her own goal in her own previous `<think>` block, not just abstractly in `[Scratchpad]`. T2 should now:
  - Recognize "this Shell result is mine"
  - Call `ReadToolOutput(id=..., offset≈149, limit=1)`
  - Say "line 150 is 150"

Pass criterion: same as scratchpad e2e — `ReadToolOutput` with offset ≈ 149 AND final Say identifies "150" as the value of line 150. Observational, eyeball the trace.

## Out of Scope

- Token-count-based eviction (turn-count is enough).
- Rolling summary of older turns (deferred — survey said it's the lowest-ROI layer for our scale).
- Per-character namespace (daemon runs one character at a time).
- Cross-process persistence (ephemeral by intent — daemon restart wipes history, just like scratchpad).
- Saving history to memsearch as long-term memory (separate concern; `NoteMemory` already handles long-term).
- Pruning intra-turn tool messages (keep full reasoning trace).

## Interaction with Scratchpad

Scratchpad and history are complementary:
- **Scratchpad** is Doll-controlled, structured working memory, persists across turns AS LONG AS she maintains it. Useful for "current goal" notes she explicitly writes.
- **History** is dispatcher-controlled, full reasoning trace, automatic. The LLM sees what Doll actually said and thought last turn.

If history alone solves T2-forgetting, scratchpad's role might shrink to "self-organized TODO". That's fine — both are cheap and complementary.

## Rollout

Single plan. Touches: new module + config + kernel + dispatcher + 1 test file + ~10 test fixture callsite updates (mirror the scratchpad plan's lessons). E2E smoke as the behavioral gate.
