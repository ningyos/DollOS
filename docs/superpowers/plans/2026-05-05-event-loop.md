# Event Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure DollOS so each raw event spawns its own concurrent task instead of running synchronously inside the IPC handler. Externally observable behavior (IPC message sequence, streaming timing) is unchanged. This step also introduces the two-tier event model (`RawEvent` / `DollEvent`) and aligns the big-LLM `user` role with `DollEvent.perception`, laying groundwork for step 5 Inner Voice perceive.

**Architecture:** New `dollos.events` module defines `RawEvent` ABC, `UserTextEvent(RawEvent)`, and `DollEvent` (perception + raw back-reference). New `dollos.dispatcher.EventDispatcher` accepts `RawEvent` via sync `dispatch()` and spawns one `asyncio.Task` per event — no worker, no queue. Each task runs `_perceive` (step-4 stub: passthrough text → DollEvent) then `_respond` (recall + LLM stream pushed into the raw event's `response_sink`, terminated with `None` sentinel). `DollOS._handle_text_input` becomes a thin shim: build sink, build `UserTextEvent`, `dispatcher.dispatch(...)`, drain sink yielding messages until `None`. Multi-event concurrency comes from `asyncio.create_task`; multi-LLM-stream concurrency is bounded by llama.cpp `--parallel 2 --cont-batching` (already in launch script).

**Tech Stack:** Python 3.12+, `asyncio`, `pytest` + `pytest-asyncio`. No new external deps.

**Spec:** `docs/superpowers/specs/2026-05-05-event-loop-design.md`

---

## Task 1: `dollos/events.py` — RawEvent / UserTextEvent / DollEvent

**Files:**
- Create: `src/dollos/events.py`
- Create: `tests/test_events.py`

Pure data shapes. No behavior.

### Step 1: Write tests first (RED)

- [ ] Create `tests/test_events.py` with these cases:
  - `test_user_text_event_holds_text_and_sink` — construct `UserTextEvent(text="hello", response_sink=asyncio.Queue())`; assert `.text == "hello"` and `.response_sink` is an `asyncio.Queue` instance.
  - `test_user_text_event_is_raw_event` — `isinstance(UserTextEvent(...), RawEvent)` is True.
  - `test_doll_event_holds_perception_and_raw` — construct a `UserTextEvent` first, then `DollEvent(perception="主人說 hi", raw=user_text_event)`; assert `.perception == "主人說 hi"` and `.raw is user_text_event`.
  - `test_doll_event_is_not_raw_event` — `isinstance(DollEvent(...), RawEvent)` is False (DollEvent is its own type, not part of RawEvent hierarchy).

Run: `uv run pytest tests/test_events.py -q` — expect ImportError (file doesn't exist).

### Step 2: Implement (GREEN)

- [ ] Create `src/dollos/events.py` per spec §5:

```python
"""Event types — two-tier model.

RawEvent: structured event from a source (IPC text, voice, timer, ...).
DollEvent: natural-language perception emitted by Inner Voice's perceive(),
           consumed by the big LLM as the `user` role.

Step 4 ships RawEvent + UserTextEvent + DollEvent dataclasses. The
RawEvent → DollEvent conversion is stubbed (passthrough). Step 5 will
replace the stub with InnerVoice.perceive().
"""

from __future__ import annotations

import asyncio
from abc import ABC
from dataclasses import dataclass

from dollos.ipc.messages import ServerMessage


class RawEvent(ABC):
    """Structured event from a source."""


@dataclass
class UserTextEvent(RawEvent):
    """Text typed by the user via IPC."""

    text: str
    response_sink: asyncio.Queue[ServerMessage | None]


@dataclass
class DollEvent:
    """Natural-language perception consumed by the big LLM as `user` role.

    perception: free-form natural language that includes source semantics.
    raw: back-reference for engineering routing (response_sink, etc).
    """

    perception: str
    raw: RawEvent
```

Run: `uv run pytest tests/test_events.py -q` — expect green.

### Step 3: Lint

- [ ] `uv run ruff check src/dollos/events.py tests/test_events.py` — no warnings.

---

## Task 2: `dollos/dispatcher.py` — EventDispatcher

**Files:**
- Create: `src/dollos/dispatcher.py`
- Create: `tests/test_dispatcher.py`

### Step 1: Write tests first (RED)

- [ ] Create `tests/test_dispatcher.py`. Tests use a fake `LLMAdapter` (yields a fixed sequence of `LLMChunk`-like objects) and a fake `InnerVoice` (returns a fixed RECALL string). All tests are `@pytest.mark.asyncio`.

  Cases:

  - `test_dispatch_is_sync_returns_immediately` — `dispatcher.dispatch(event)` returns a value (None) synchronously; immediately after the call, the underlying task is registered (`len(dispatcher._tasks) == 1`) and not yet completed (it's still running the fake adapter / IV which we make slow with `await asyncio.sleep(0)` cycles or by giving the fake adapter a controllable async generator).
  - `test_dispatch_pushes_chunks_then_turnend_then_none_sentinel` — fake adapter yields 2 text chunks then `done`; fake IV returns `"RECALL:\n- foo\n"`. Dispatch a `UserTextEvent` with a sink; drain sink with timeout. Expect: `[TextChunk("..."), TextChunk("..."), TurnEnd(), None]`.
  - `test_recall_passes_perception_to_iv_and_perception_to_adapter_user` — capture args from fake IV and fake adapter. Assert IV.recall called with `event.text` (since step-4 perceive is passthrough). Assert adapter.stream_completion called with `user=event.text` and `prefill="RECALL:\n- foo\nGOAL: "`.
  - `test_handler_exception_pushes_errormsg_and_sentinel` — fake IV.recall raises `RuntimeError("boom")`. Dispatch event; drain sink. Expect: `[ErrorMsg(...), None]`. ErrorMsg.message contains `"boom"` substring.
  - `test_unknown_raw_event_pushes_errormsg` — define a local `class FooEvent(RawEvent): pass` (no fields needed for the test, but RawEvent is ABC so add minimal fields if needed — actually since RawEvent has no abstract methods, plain subclass works). The dispatcher's `_sink_of` will raise `TypeError`; the `_handle` method's bare `except Exception` will catch it. **However:** since `_sink_of` is called BEFORE the try block, the TypeError will propagate out of `_handle` and into the task, where asyncio will log it. Update spec/code: move `_sink_of` call inside try OR handle before try. Choose: call `_sink_of` first (outside try), and on TypeError raise it as the task's exception (visible in logs but no sink available to push errors). Test asserts: `await asyncio.sleep(0.05)` for task to finish, then `dispatcher._tasks` is empty (task completed/discarded), and the task's exception was a `TypeError`. Get the task before discard via callback or by passing an event that we register a callback on... easier: capture log via caplog and assert TypeError logged.
    - **Better redesign:** since `_sink_of` raising means we can't push to a sink anyway, and an unsupported RawEvent IS a programming bug (caller shouldn't dispatch it), let it crash the task and log. Update §6 spec accordingly: `_handle` body is `sink = self._sink_of(raw)` (may raise), then try/except for everything else.
  - `test_stop_cancels_in_flight_tasks` — dispatch an event whose handler hangs (fake adapter that awaits an `asyncio.Event` never set). Then `await dispatcher.stop()` with a timeout; assert it returns within ~50ms. Assert the task is cancelled.
  - `test_dispatch_after_stop_raises_runtime_error` — `await dispatcher.stop()` then `dispatcher.dispatch(...)` raises `RuntimeError`.
  - `test_concurrent_dispatch_runs_in_parallel` — two `UserTextEvent` with separate sinks. Fake adapter that records start time per call and yields chunks after a small `asyncio.sleep(0.05)`. Dispatch both back-to-back. Drain both sinks. Assert both completed AND elapsed wall-time is closer to single-call (~0.05s) than serial (~0.10s) — give a margin (< 0.09s wall-time for both).

  Skip the `_sink_of` raises test if it's awkward — instead test that `_handle` recovers:
  - `test_perceive_raises_typeerror_for_unsupported_raw` — define `class FooEvent(RawEvent): pass`. Dispatch foo. Wait for task. Assert task ended (regardless of sink, since FooEvent has no sink) and that an exception was logged (use caplog).

Run: `uv run pytest tests/test_dispatcher.py -q` — expect ImportError.

### Step 2: Implement (GREEN)

- [ ] Create `src/dollos/dispatcher.py` per spec §6, with these refinements clarified during test design:

  - `_handle(raw)` order:
    1. `try: sink = self._sink_of(raw)` outside the main except — if sink resolution fails, we have nothing to push to, just let the task die with the exception (asyncio will log).
    2. Inside try/except/finally: `_perceive`, `_respond`, except → push `ErrorMsg`, finally → push `None`.
  - Actually simpler: ALWAYS try `_sink_of` first; if it raises, log and return (task dies cleanly). If it succeeds, run the rest in try/except/finally.

  ```python
  async def _handle(self, raw: RawEvent) -> None:
      try:
          sink = self._sink_of(raw)
      except TypeError:
          logger.exception("no sink for raw event")
          return
      try:
          doll_event = await self._perceive(raw)
          await self._respond(doll_event, sink)
      except asyncio.CancelledError:
          raise
      except Exception as e:
          logger.exception("dispatcher _handle error")
          sink.put_nowait(ErrorMsg(message=f"handler error: {e}"))
      finally:
          sink.put_nowait(None)
      ```

  - `_perceive(raw)` step-4 stub: `if isinstance(raw, UserTextEvent): return DollEvent(perception=raw.text, raw=raw); raise TypeError(...)` — if `_perceive` raises TypeError for an unsupported raw type, the outer try catches it as Exception and pushes ErrorMsg. (For UserTextEvent the sink exists, so this path is fine.)
  - `dispatch()` is `def`, not `async def`. Body:
    ```python
    if self._stopping:
        raise RuntimeError("EventDispatcher is stopping")
    task = asyncio.create_task(self._handle(raw), name=f"event-{type(raw).__name__}")
    self._tasks.add(task)
    task.add_done_callback(self._tasks.discard)
    ```
  - `stop()` per spec: cancel all tasks, gather with `return_exceptions=True`.

Run: `uv run pytest tests/test_dispatcher.py -q` — expect green. Iterate on failures; the `concurrent dispatch parallel` test may need timing margin tuning.

### Step 3: Lint

- [ ] `uv run ruff check src/dollos/dispatcher.py tests/test_dispatcher.py`.

---

## Task 3: Wire EventDispatcher into kernel

**Files:**
- Modify: `src/dollos/kernel.py`
- Modify: `tests/test_kernel.py` (or create if not present — check first)

### Step 1: Survey existing kernel test

- [ ] `ls tests/` and read whichever kernel/integration test exists. Look for the pattern used to construct `DollOS` with fakes (likely monkeypatched factories or constructor injection of fake adapter/IV). Mirror that pattern.

### Step 2: Update / write tests (RED)

- [ ] Add or modify `tests/test_kernel.py`:

  - `test_handle_text_input_yields_chunks_then_turnend` — construct `DollOS` with fake adapter (yields 2 text chunks then `done`), fake InnerVoice (returns `"RECALL:\n- foo\n"`). Call `_handle_text_input(TextInput(text="hi"))` and collect into a list (consume the async iterator). Assert sequence is `[TextChunk("..."), TextChunk("..."), TurnEnd()]`. (No `None` — that's the internal sentinel, not yielded.)
  - `test_handle_text_input_yields_errormsg_on_dispatch_failure` — fake InnerVoice that raises; call handler; collected sequence is `[ErrorMsg(...)]`.
  - `test_dispatch_user_text_uses_recall_in_prefill` — fake InnerVoice returns `"RECALL:\n- foo\n"`; capture `prefill` arg passed to fake adapter; assert `prefill == "RECALL:\n- foo\nGOAL: "`.
  - `test_dispatch_user_text_uses_text_as_user_role` — capture `user` arg passed to fake adapter; assert `user == "hi"` (step-4 stub passthrough).

Run: `uv run pytest tests/test_kernel.py -q` — expect failure on the first new test.

### Step 3: Refactor kernel (GREEN)

- [ ] Modify `src/dollos/kernel.py` per spec §7:
  - Add imports: `from dollos.events import UserTextEvent`, `from dollos.dispatcher import EventDispatcher`.
  - In `DollOS.__init__`: build `self.dispatcher = EventDispatcher(adapter=..., inner_voice=..., renderer=..., character_profile=...)`.
  - Replace `_handle_text_input` body with the thin shim from spec §7:
    ```python
    sink: asyncio.Queue[ServerMessage | None] = asyncio.Queue()
    self.dispatcher.dispatch(UserTextEvent(text=msg.text, response_sink=sink))
    while True:
        item = await sink.get()
        if item is None:
            return
        yield item
    ```
  - Remove the old `_handle_text_input` body that called recall + adapter directly (it's now in `EventDispatcher._respond`).
  - In `run()` `finally` block (after `await self.server.stop()`), add `await self.dispatcher.stop()`.

Run: `uv run pytest -q` — full suite green.

### Step 4: Sanity check

- [ ] Re-read `kernel.py`. `_handle_text_input` should be ~6 logical lines. If longer, you put logic in the wrong place.
- [ ] Confirm no recall/render/adapter call survives directly inside `kernel.py`'s handler — they all live in `EventDispatcher._respond` now.
- [ ] `uv run ruff check src/dollos/kernel.py tests/test_kernel.py`.

---

## Task 4: Manual smoke test

**Files:** none modified

This is the human-in-the-loop verification. Only attempt if Tasks 1-3 are green.

- [ ] Confirm two llama-server processes are running (big model on 8001, embedding on 8002). If not, ask the user to start them.
- [ ] Run `uv run python -m dollos --config config.toml`.
- [ ] Connect a WS client (any minimal `websockets` client works) and send a `TextInput` JSON message.
- [ ] Verify:
  - Connection succeeds.
  - Server responds with a stream of `TextChunk` then a `TurnEnd`.
  - Stream timing feels comparable to step 3.
  - Daemon log shows `event-UserTextEvent` task name appearing (asyncio task name) — quick sanity that the dispatcher path is taken.
- [ ] Send a second `TextInput` immediately after the first completes; confirm normal response (dispatcher still alive, not stuck in `_stopping`).
- [ ] Optional concurrency check: open two WS connections and send turns near-simultaneously. Confirm both complete (may stream interleaved due to llama.cpp `--parallel 2`).
- [ ] If anything misbehaves, do NOT mark this plan complete. File the symptom and stop.

---

## Verification checklist (before marking plan complete)

- [ ] `uv run pytest -q` — all green
- [ ] `uv run ruff check src/dollos tests` — no warnings
- [ ] Manual smoke test passes (Task 4)
- [ ] `_handle_text_input` is now a thin shim (~6 lines)
- [ ] `EventDispatcher._respond` body is recognizable as the lifted step-3 logic, not a rewrite
- [ ] No new external dependencies in `pyproject.toml`
- [ ] No changes to `src/dollos/ipc/`, `src/dollos/llm/`, `src/dollos/inner_voice.py`, `src/dollos/prompts/`, `src/dollos/config.py`, `config.example.toml`
- [ ] `EventDispatcher.dispatch` is sync (not `async def`)
- [ ] `RawEvent` is ABC, `UserTextEvent` subclasses it; `DollEvent` is a separate dataclass (NOT a RawEvent)
