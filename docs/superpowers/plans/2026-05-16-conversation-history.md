# Conversation History (Sliding Window) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a turn-count-bounded sliding window of recent conversation turns. After each Doll cascade ends, the dispatcher captures the turn's full message list. On the next turn, recent turns are prepended to the LLM messages so Doll sees her own prior reasoning trace. Solves the cross-turn agency / continuity gap that scratchpad partially closed.

**Architecture:** A `ConversationHistory` class with `add_turn` / `recent_messages` / `clear`. Lives on `EventDispatcher`. Subagent spawns a fresh independent history. Each cascade END stores the turn's messages; each cascade START reads and prepends recent.

**Tech Stack:** Python 3.13, asyncio, pydantic, pytest. No new third-party deps.

**Spec:** `docs/superpowers/specs/2026-05-16-conversation-history-design.md`.

---

## File Structure

- **Create:** `src/dollos/conversation_history.py` — `ConversationHistory` class.
- **Modify:** `src/dollos/config.py` — add `ConversationHistoryConfig` field.
- **Modify:** `config.example.toml` — new `[conversation_history]` section.
- **Modify:** `src/dollos/kernel.py` — instantiate `ConversationHistory`, pass to dispatcher.
- **Modify:** `src/dollos/dispatcher.py` — `EventDispatcher.__init__` takes required `conversation_history` kwarg; cascade reads recent at start, stores turn at end.
- **Create:** `tests/test_conversation_history.py` — unit tests.
- **Modify:** `tests/test_kernel.py` — wiring test.
- **Modify:** `tests/test_dispatcher_*.py` — turn-2 carries turn-1 messages assertion.
- **Modify:** `tests/_dispatcher_helpers.py`, `tests/test_subagent.py`, etc. — pass `ConversationHistory()` to `EventDispatcher` / `SubagentRunner` callsites.
- **Create:** `scripts/smoke_doll_conversation_history_e2e.py` — third smoke after paging + scratchpad.

---

## Task 1: ConversationHistory class + unit tests

**Files:**
- Create: `src/dollos/conversation_history.py`
- Test: `tests/test_conversation_history.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_conversation_history.py
from dollos.conversation_history import ConversationHistory


def test_initial_empty() -> None:
    h = ConversationHistory(max_turns=3)
    assert h.turn_count() == 0
    assert h.recent_messages() == []


def test_add_turn_then_recent_messages_round_trip() -> None:
    h = ConversationHistory(max_turns=3)
    h.add_turn([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
    assert h.turn_count() == 1
    assert h.recent_messages() == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_multiple_turns_flatten_in_order() -> None:
    h = ConversationHistory(max_turns=10)
    h.add_turn([{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}])
    h.add_turn([{"role": "user", "content": "u2"}, {"role": "assistant", "content": "a2"}])
    msgs = h.recent_messages()
    assert [m["content"] for m in msgs] == ["u1", "a1", "u2", "a2"]


def test_cap_drops_oldest() -> None:
    h = ConversationHistory(max_turns=2)
    h.add_turn([{"role": "user", "content": "u1"}])
    h.add_turn([{"role": "user", "content": "u2"}])
    h.add_turn([{"role": "user", "content": "u3"}])
    assert h.turn_count() == 2
    assert [m["content"] for m in h.recent_messages()] == ["u2", "u3"]


def test_empty_messages_ignored() -> None:
    h = ConversationHistory(max_turns=3)
    h.add_turn([])
    assert h.turn_count() == 0


def test_clear_resets() -> None:
    h = ConversationHistory(max_turns=3)
    h.add_turn([{"role": "user", "content": "x"}])
    h.clear()
    assert h.turn_count() == 0
    assert h.recent_messages() == []


def test_add_turn_defensive_copy() -> None:
    h = ConversationHistory(max_turns=3)
    msgs = [{"role": "user", "content": "original"}]
    h.add_turn(msgs)
    # Mutating original after add should not affect stored
    msgs.append({"role": "user", "content": "leak"})
    assert h.recent_messages() == [{"role": "user", "content": "original"}]
```

- [ ] **Step 2: Run, verify failure**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/conversation-history
uv run pytest tests/test_conversation_history.py -v
```
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement ConversationHistory**

```python
# src/dollos/conversation_history.py
"""ConversationHistory — bounded sliding window of recent turn transcripts.

Each "turn" is the full LLM message list (excluding the system message)
from one cascade. New turns are prepended to the LLM message list before
send so the model sees recent reasoning.

See docs/superpowers/specs/2026-05-16-conversation-history-design.md.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class ConversationHistory:
    """In-memory bounded window of recent turn transcripts.

    Each `add_turn(messages)` appends a complete message list from a
    finished cascade. `recent_messages()` flattens all retained turns
    into a single list suitable for prepending before a new user
    message. Storage is FIFO bounded at `max_turns`.
    """

    def __init__(self, max_turns: int = 6) -> None:
        if max_turns < 1:
            raise ValueError(f"max_turns must be >= 1, got {max_turns}")
        self._max_turns = max_turns
        self._turns: list[list[dict]] = []

    def add_turn(self, messages: list[dict]) -> None:
        """Append a turn's full message list; drop oldest if over cap.

        Empty message lists are ignored (no-op).
        """
        if not messages:
            return
        # Defensive copy so subsequent caller mutations don't leak in.
        self._turns.append(list(messages))
        if len(self._turns) > self._max_turns:
            dropped = len(self._turns) - self._max_turns
            self._turns = self._turns[dropped:]

    def recent_messages(self) -> list[dict]:
        """Flatten retained turns into a single message list."""
        out: list[dict] = []
        for turn in self._turns:
            out.extend(turn)
        return out

    def turn_count(self) -> int:
        return len(self._turns)

    def clear(self) -> None:
        self._turns.clear()
```

- [ ] **Step 4: Run, verify pass**

```bash
uv run pytest tests/test_conversation_history.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dollos/conversation_history.py tests/test_conversation_history.py
git commit -m "feat(conversation-history): ConversationHistory bounded turn buffer"
```

---

## Task 2: Config — ConversationHistoryConfig + config.example.toml

**Files:**
- Modify: `src/dollos/config.py`
- Modify: `config.example.toml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing test**

In `tests/test_config.py`:

```python
def test_settings_includes_conversation_history_default() -> None:
    from dollos.config import Settings
    settings = Settings.model_validate({
        "llm": {"provider": "llamacpp", "template": "qwen3-thinking",
                "base_url": "http://x", "model_alias": "m"},
        "ipc": {"host": "127.0.0.1", "port": 0},
        "log": {"level": "INFO"},
        "data": {"root": "/tmp/x"},
        "memsearch": {"top_k": 10},
        "character": {"pack": "character_packs/gura"},
        "inner_voice": {"base_url": "http://y", "timeout_s": 5.0},
    })
    assert settings.conversation_history.max_turns == 6


def test_settings_conversation_history_custom_max_turns() -> None:
    from dollos.config import Settings
    settings = Settings.model_validate({
        "llm": {"provider": "llamacpp", "template": "qwen3-thinking",
                "base_url": "http://x", "model_alias": "m"},
        "ipc": {"host": "127.0.0.1", "port": 0},
        "log": {"level": "INFO"},
        "data": {"root": "/tmp/x"},
        "memsearch": {"top_k": 10},
        "character": {"pack": "character_packs/gura"},
        "inner_voice": {"base_url": "http://y", "timeout_s": 5.0},
        "conversation_history": {"max_turns": 10},
    })
    assert settings.conversation_history.max_turns == 10
```

- [ ] **Step 2: Run, verify failure**

```bash
uv run pytest tests/test_config.py -k "conversation_history" -v
```
Expected: AttributeError — Settings has no `conversation_history` field.

- [ ] **Step 3: Add ConversationHistoryConfig to config.py**

In `src/dollos/config.py`:

```python
class ConversationHistoryConfig(BaseModel):
    max_turns: int = Field(
        6,
        ge=1,
        le=50,
        description="conversation window size in turns; default 6 (industry standard tail buffer)",
    )


class Settings(BaseModel):
    ...existing fields unchanged...
    conversation_history: ConversationHistoryConfig = Field(default_factory=ConversationHistoryConfig)
```

Place `ConversationHistoryConfig` near the existing per-section config models (e.g. after `MemsearchConfig`). Place the field in `Settings` near other per-subsystem fields.

- [ ] **Step 4: Run test, verify pass**

```bash
uv run pytest tests/test_config.py -v
```
Expected: all PASS.

- [ ] **Step 5: Update config.example.toml**

In `config.example.toml`, add (place near `[memsearch]`):

```toml
# ----------------------------------------------------------------------
# Conversation history sliding window — recent turn transcripts prepended
# to every LLM call so Doll sees her own prior reasoning across turns.
# Default 6; tune for context budget if cascades are long.
# ----------------------------------------------------------------------

[conversation_history]
max_turns = 6
```

- [ ] **Step 6: Run full suite**

```bash
uv run pytest -q
```
Expected: same pass count plus 2 new config tests.

- [ ] **Step 7: Commit**

```bash
git add src/dollos/config.py config.example.toml tests/test_config.py
git commit -m "feat(conversation-history): ConversationHistoryConfig with max_turns"
```

---

## Task 3: Wire ConversationHistory into kernel + dispatcher + subagent

**Files:**
- Modify: `src/dollos/kernel.py`
- Modify: `src/dollos/dispatcher.py`
- Modify: `src/dollos/subagent.py`
- Test: `tests/test_kernel.py`

Lesson from scratchpad Task 2: required field, no Optional, no None default. Update all `EventDispatcher(...)` and `SubagentRunner(...)` test callsites to pass a `ConversationHistory()` instance.

- [ ] **Step 1: Add conversation_history kwarg to EventDispatcher**

In `src/dollos/dispatcher.py`:

```python
from dollos.conversation_history import ConversationHistory  # top-level import

class EventDispatcher:
    def __init__(
        self,
        *,
        ...existing args,
        scratchpad: Scratchpad,
        conversation_history: ConversationHistory,  # NEW required
    ) -> None:
        ...
        self._scratchpad = scratchpad
        self._conversation_history = conversation_history
```

(Don't wire into ToolCtx — history is dispatcher-managed, tools don't access it.)

**NOTE: Subagent is NOT wired with ConversationHistory.** Subagent cascades are single-turn (one task → Report → end), so there are no cross-turn boundaries to bridge. The subagent's own accumulating messages list within `_run_cascade` is already its full context. Skip subagent.

- [ ] **Step 2: Kernel instantiates ConversationHistory and passes to dispatcher**

In `src/dollos/kernel.py`:

```python
from dollos.conversation_history import ConversationHistory  # top-level import

class DollOS:
    def __init__(self, settings: Settings) -> None:
        ...
        self._conversation_history = ConversationHistory(
            max_turns=settings.conversation_history.max_turns,
        )
        ...
        self._dispatcher = EventDispatcher(
            ...,
            scratchpad=self._scratchpad,
            conversation_history=self._conversation_history,  # NEW
        )
```

- [ ] **Step 3: Update test fixtures**

Grep for all `EventDispatcher(` callsites in tests:

```bash
grep -rn "EventDispatcher(" tests/ src/dollos/ --include="*.py"
```

For each test fixture, add `conversation_history=ConversationHistory()` (and import `ConversationHistory` at top of touched test files).

Mirror what was done in scratchpad Task 2 — same set of files likely needs updating (excluding subagent tests since subagent isn't wired).

- [ ] **Step 4: Write kernel wiring test**

In `tests/test_kernel.py`:

```python
def test_kernel_has_conversation_history() -> None:
    settings = _make_minimal_settings()
    dollos = DollOS(settings)
    from dollos.conversation_history import ConversationHistory
    assert isinstance(dollos._conversation_history, ConversationHistory)
    assert dollos._conversation_history.turn_count() == 0


def test_kernel_uses_configured_max_turns() -> None:
    # Tweak settings.conversation_history.max_turns; assert it propagates
    settings = _make_minimal_settings()
    settings = settings.model_copy(update={"conversation_history": {"max_turns": 10}})
    # Or however your test infra updates settings; alternative:
    # build settings dict manually with max_turns=10
    dollos = DollOS(settings)
    assert dollos._conversation_history._max_turns == 10
```

(Adjust to whatever the test infra prefers.)

- [ ] **Step 5: Run full suite**

```bash
uv run pytest -q
```
Expected: all pass; new failures are missed test fixture callsites — fix.

- [ ] **Step 6: Commit**

```bash
git add src/dollos/kernel.py src/dollos/dispatcher.py tests/
git commit -m "feat(conversation-history): wire ConversationHistory into kernel + dispatcher"
```

---

## Task 4: Dispatcher reads + writes history around each cascade

**Files:**
- Modify: `src/dollos/dispatcher.py`
- Test: `tests/test_dispatcher_perception.py` or new `tests/test_dispatcher_history.py`

This is the heart of the feature. At cascade start, dispatcher prepends `history.recent_messages()` between system prompt and the new user message. At cascade end, dispatcher snapshots the final message list (excluding system) into history.

- [ ] **Step 1: Find the cascade entry point**

```bash
grep -n "messages\s*=\s*\[.*system\|self._cascade\|cascade.run\|_respond" src/dollos/dispatcher.py | head -10
```

Identify where the LLM `messages` list is built per turn. Find both the BUILD-START (where `[system, user_msg]` is constructed) and CASCADE-END (where the cascade finishes — typically where the for-loop / while-loop exits or where the final assistant message is appended).

- [ ] **Step 2: Write failing test for history-prepend behavior**

In `tests/test_dispatcher_history.py` (new file or add to existing perception tests):

```python
import pytest

from dollos.conversation_history import ConversationHistory
from dollos.dispatcher import EventDispatcher
# plus any imports needed for _make_dispatcher / fixtures


@pytest.mark.asyncio
async def test_dispatcher_prepends_history_messages_on_turn_2(_make_dispatcher):
    history = ConversationHistory(max_turns=5)
    history.add_turn([
        {"role": "user", "content": "turn 1 user message"},
        {"role": "assistant", "content": "turn 1 doll response"},
    ])
    dispatcher = _make_dispatcher(conversation_history=history)

    # Capture the messages list passed to the LLM (mock the LLM client / cascade runner)
    captured_messages = []
    # ...whatever mocking infra exists for capturing LLM calls

    # Simulate turn 2: dispatch a user text event
    # ...

    # Assert: messages now look like
    #   [system, ...history flattened..., new user message]
    assert captured_messages[0]["role"] == "system"
    assert captured_messages[1]["content"] == "turn 1 user message"
    assert captured_messages[2]["content"] == "turn 1 doll response"
    assert captured_messages[3]["role"] == "user"   # new turn 2 user msg
```

This test depends heavily on the existing dispatcher test infrastructure. If creating a clean black-box test is hard, fall back to white-box: directly inspect `messages` list after the dispatcher builds it. Either way, the assertion is "history messages appear between system and new user message in order."

- [ ] **Step 3: Write failing test for cascade-end snapshot**

```python
@pytest.mark.asyncio
async def test_dispatcher_stores_turn_in_history_on_cascade_end(_make_dispatcher):
    history = ConversationHistory(max_turns=5)
    dispatcher = _make_dispatcher(conversation_history=history)

    # Run a turn (UserTextEvent) and wait for it to complete
    # ...

    # After cascade ends, history should have 1 turn
    assert history.turn_count() == 1
    msgs = history.recent_messages()
    # The first message should be the user perception
    assert msgs[0]["role"] == "user"
    # Some assistant message should be present
    assert any(m["role"] == "assistant" for m in msgs)
```

- [ ] **Step 4: Run, verify failure**

```bash
uv run pytest tests/test_dispatcher_history.py -v
```
Expected: FAIL.

- [ ] **Step 5: Implement prepend on cascade start**

In dispatcher's cascade entry point, change:

```python
messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": first_user},
]
```

to:

```python
history_messages = self._conversation_history.recent_messages()
messages = [
    {"role": "system", "content": system_prompt},
    *history_messages,
    {"role": "user", "content": first_user},
]
```

- [ ] **Step 6: Implement store on cascade end**

After the cascade's inner loop exits (and before returning / before `_on_cascade_end`), add:

```python
# Snapshot full turn (everything except the system message)
turn_messages = messages[1:]
self._conversation_history.add_turn(turn_messages)
```

Make sure this runs on BOTH successful cascade end AND error/exception paths (per spec: store partial messages too). If the cascade is wrapped in try/finally, put the add_turn call in `finally`.

- [ ] **Step 7: Run history tests + full suite**

```bash
uv run pytest tests/test_dispatcher_history.py -v
uv run pytest -q
```
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/dollos/dispatcher.py tests/test_dispatcher_history.py
git commit -m "feat(conversation-history): prepend recent turns + snapshot on cascade end"
```

---

## Task 5: Real-LLM e2e smoke

**Files:**
- Create: `scripts/smoke_doll_conversation_history_e2e.py`

Third smoke building on paging + scratchpad. Same "find line 150" prompt; expectation is now that history alone (without relying on Doll to write scratchpad) carries enough context for T2 to recognize her own action's result.

- [ ] **Step 1: Write smoke script**

```python
# scripts/smoke_doll_conversation_history_e2e.py
"""Real-LLM e2e: conversation history solves T2-forgetting.

Requires:
- Big LLM at http://127.0.0.1:8001
- Inner Voice small LLM at http://127.0.0.1:8003

Sends Doll: "I need you to do this for me: run `seq 1 200` in a shell.
After the result comes back, look at line 150 specifically and tell me
what it is."

Expected: T2 sees T1's full reasoning (her own <think> + Shell call) in
the message history, so she recognizes the incoming ShellResult as her
own action and calls ReadToolOutput to fetch line 150.

Pass criterion: ReadToolOutput call with offset ≈ 149, final Say
identifies "150" as the value of line 150. Observational, not pytest.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import date
from pathlib import Path

import websockets

from dollos.config import (
    CharacterConfig, ConversationHistoryConfig, DataConfig,
    InnerVoiceConfig, IPCConfig, LLMConfig, LogConfig,
    MemsearchConfig, Settings,
)
from dollos.kernel import DollOS


PROMPT = (
    "I need you to do this for me: run `seq 1 200` in a shell. "
    "After the result comes back, look at line 150 specifically and tell me what it is."
)
LOG_PATH = Path("/tmp/iv_doll_conversation_history_e2e.log")


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="smoke-history-"))
    pack_dir = Path("character_packs/gura")

    settings = Settings(
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://127.0.0.1:8001",
            model_alias="unsloth/Qwen3.6",
        ),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        log=LogConfig(level="INFO"),
        data=DataConfig(root=tmp / "data"),
        memsearch=MemsearchConfig(top_k=10),
        character=CharacterConfig(pack=pack_dir),
        inner_voice=InnerVoiceConfig(
            base_url="http://127.0.0.1:8003",
            timeout_s=30.0,
        ),
        conversation_history=ConversationHistoryConfig(max_turns=6),
    )
    dollos = DollOS(settings)
    dollos._bootstrapped_dates.add(date.today())

    await dollos.memsearch.index()
    await dollos.server.start()

    received: list[dict] = []
    port = dollos.server.port
    uri = f"ws://127.0.0.1:{port}"

    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "text_input", "text": PROMPT}))

        end_count = 0
        try:
            while end_count < 3:
                raw = await asyncio.wait_for(ws.recv(), timeout=120.0)
                msg = json.loads(raw)
                received.append(msg)
                if msg["type"] == "turn_end":
                    end_count += 1
        except asyncio.TimeoutError:
            print(f"timeout after {end_count} turns")

    LOG_PATH.write_text(json.dumps(received, indent=2, ensure_ascii=False))
    print(f"trace saved: {LOG_PATH}")

    tool_calls = [m for m in received if m.get("type") == "tool_call"]
    print(f"\n=== {len(tool_calls)} tool calls ===")
    for tc in tool_calls:
        print(f"  {tc.get('name')}: {json.dumps(tc.get('arguments', {}), ensure_ascii=False)[:140]}")

    says = [m for m in received if m.get("type") == "say"]
    print(f"\n=== {len(says)} say messages ===")
    for s in says:
        print(f"  {s.get('text', '')[:200]}")

    print(f"\nhistory turn count at end: {dollos._conversation_history.turn_count()}")

    await dollos.server.stop()
    print("\nDONE — eyeball the log to judge whether history solved T2 forgetting.")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Verify llama-servers are running**

```bash
curl -s -m 2 http://127.0.0.1:8001/health
curl -s -m 2 http://127.0.0.1:8003/health
```

If either is down, report BLOCKED — do not try to start them.

- [ ] **Step 3: Run smoke**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/conversation-history
uv run python scripts/smoke_doll_conversation_history_e2e.py
```

- [ ] **Step 4: Observe behavior**

Inspect trace + summary print:

1. T1: did Doll fire Shell? (yes expected)
2. T2 (ShellResultEvent's new turn): what does the LLM message list look like? Does it now contain T1's user msg + Doll's assistant message? Print or grep the trace log.
3. T2: did Doll call ReadToolOutput with offset ≈ 149? (key pass criterion)
4. Final Say: does it identify "150" as line 150's value?

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke_doll_conversation_history_e2e.py
git commit -m "feat(conversation-history): real-LLM e2e smoke"
```

---

## Final verification

- [ ] **Step 1: Full suite green**

```bash
uv run pytest -q
```

- [ ] **Step 2: Smoke runs end-to-end with desired behavior**

```bash
uv run python scripts/smoke_doll_conversation_history_e2e.py
```

- [ ] **Step 3: Use superpowers:finishing-a-development-branch**
