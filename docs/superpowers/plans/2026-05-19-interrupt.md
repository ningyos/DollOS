# Plan 3: Interrupt — Say cancel + cascade preempt

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the user speaks (or types) while Doll is mid-cascade, stop her voice immediately and abort the current cascade cleanly so she can switch to the new input. Two semantics:
- **Say cancel** — abort current TTS playback (cancel worker task + drain queue).
- **Cascade preempt** — clean exit at next iter / LLM-chunk boundary. External actions (Shell / Subagent / Monitor) keep running; their results arrive later as perceptions.

**Architecture:**
- `CascadeCtx` carries a `cancel_event: asyncio.Event`.
- `MindLoop` tracks the active `CascadeCtx` (one at a time) and exposes `cancel_current_cascade()`.
- `mind_loop._llm_iterate` checks the event between LLM chunks and tool dispatches; if set, the loop returns cleanly.
- `VoiceSession.abort_speak()` cancels the active speak worker task + drains the queue + restarts the worker dormant.
- Kernel's `_handle_message` for `TextInput`: if a cascade is active, call `mind_loop.cancel_current_cascade()` + `voice_session.abort_speak()` BEFORE pushing the new UserSpoke perception. Also handle explicit `Interrupt` IPC.
- New `Interrupt` IPC client message + `SayAborted` server message.
- New `Interrupted` perception so Doll knows her previous turn was cut short.
- Scaffolding adds a short "Interrupts" section.

**Tech Stack:** Python 3.13, asyncio.

**Out of scope:**
- SIGTERM in-flight Shell or Subagent processes (they keep running; results land as normal perceptions later)
- WebRTC outbound buffer flush / fade-out audio (best-effort: client just hears silence after the last queued PCM chunk drains, ~100ms gap)
- LLM HTTP stream cancellation at the socket level — we exit the consumer loop, which lets the response complete on the server side and gets discarded client-side. This is acceptable; full HTTP cancel can be added later if needed.
- **Pre-LLM iterate() setup is not cancellable.** `iterate()` runs perception drain → memsearch query (~50-200ms) → snapshot blocks → prompt render BEFORE `_llm_iterate`. Cancel signal arriving in this window takes effect only when LLM streaming starts. Practically <300ms latency, acceptable.

---

## File Structure

**New files:**
- `src/dollos/cascade/cascade_ctx.py` — `CascadeCtx` dataclass
- `tests/test_cascade_ctx.py`
- `tests/test_interrupt.py` — integration test for full cancel flow
- `scripts/smoke_interrupt.py` — E2E smoke

**Modified:**
- `src/dollos/mind/mind_loop.py` — add `_cascade_ctx: CascadeCtx | None`; `cancel_current_cascade()`; check cancel in `_llm_iterate` between chunks + before tool dispatch + after each parser flush
- `src/dollos/voice/session.py` — add `abort_speak()` method
- `src/dollos/voice/sink.py` — no change (sink stays simple)
- `src/dollos/ipc/messages.py` — add `Interrupt` client + `SayAborted` server messages
- `src/dollos/ipc/server.py` / `kernel.py` — handle `Interrupt`; auto-interrupt on TextInput mid-cascade
- `src/dollos/events.py` — add `kind="Interrupted"` perception render (just a string, not a new RawEvent)
- `src/dollos/mind/mind_prompt.py` — render Interrupted perception body
- `src/dollos/prompts/templates/scaffolding.jinja` — short "Interrupts" section
- `tests/test_voice_session_speak_queue.py` — extend with abort_speak tests
- `tests/test_mind_loop.py` — extend with cancel tests

---

## Task 1: CascadeCtx with cancel_event

**Files:**
- Create: `src/dollos/cascade/cascade_ctx.py`
- Create: `tests/test_cascade_ctx.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_cascade_ctx.py
import asyncio
import pytest


def test_cascade_ctx_starts_uncancelled():
    from dollos.cascade.cascade_ctx import CascadeCtx
    ctx = CascadeCtx()
    assert not ctx.cancelled


def test_cascade_ctx_cancel_sets_flag():
    from dollos.cascade.cascade_ctx import CascadeCtx
    ctx = CascadeCtx()
    ctx.cancel()
    assert ctx.cancelled


@pytest.mark.asyncio
async def test_cascade_ctx_wait_cancelled_blocks_until_cancel():
    from dollos.cascade.cascade_ctx import CascadeCtx
    ctx = CascadeCtx()
    waiter = asyncio.create_task(ctx.wait_cancelled())
    await asyncio.sleep(0.02)
    assert not waiter.done()
    ctx.cancel()
    await asyncio.wait_for(waiter, timeout=1.0)
    assert waiter.done()
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

```python
# src/dollos/cascade/cascade_ctx.py
"""CascadeCtx — owns the cancel signal for one cascade run."""
from __future__ import annotations

import asyncio


class CascadeCtx:
    """Single-cascade cancel signal.

    Created by mind_loop at the start of an iter; held until the iter
    returns. External callers (kernel on new TextInput) set the cancel
    via mind_loop.cancel_current_cascade() — propagates through this
    Event so the in-flight LLM stream / dispatch loop returns cleanly
    at the next checkpoint.
    """

    def __init__(self) -> None:
        self._cancel_event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def cancel(self) -> None:
        self._cancel_event.set()

    async def wait_cancelled(self) -> None:
        # Not used by Plan 3; reserved for future cancel-aware awaits inside cascade.
        await self._cancel_event.wait()
```

- [ ] **Step 4: Pass**

- [ ] **Step 5: Commit**

```bash
git add src/dollos/cascade/cascade_ctx.py tests/test_cascade_ctx.py
git commit -m "feat(cascade): CascadeCtx with cancel_event"
```

---

## Task 2: mind_loop tracks active cascade + cancel propagation

**Files:**
- Modify: `src/dollos/mind/mind_loop.py`
- Extend: `tests/test_mind_loop.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_mind_loop.py — append

@pytest.mark.asyncio
async def test_cancel_current_cascade_exits_llm_stream():
    """When mind_loop.cancel_current_cascade() is called mid-stream,
    _llm_iterate exits cleanly before the stream finishes."""
    # Mock the LLM to yield slow chunks (sleep between)
    # Start _llm_iterate as a task
    # After 0.1s, call mind_loop.cancel_current_cascade()
    # Assert task completes within reasonable time (not 60s timeout)
    # Assert NOT all chunks were consumed
    ...


@pytest.mark.asyncio
async def test_cancel_before_iter_no_op():
    """Calling cancel when no cascade is active is a no-op."""
    # Construct mind_loop fixture, call cancel_current_cascade()
    # Assert no exception
    ...
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

In `src/dollos/mind/mind_loop.py`:

```python
# Top imports
from dollos.cascade.cascade_ctx import CascadeCtx

# In __init__:
self._cascade_ctx: CascadeCtx | None = None

# Add public API:
@property
def is_cascade_active(self) -> bool:
    """True iff _llm_iterate is currently running (a cascade is in flight)."""
    return self._cascade_ctx is not None

def cancel_current_cascade(self) -> None:
    """Set cancel on the active cascade context, if any. No-op otherwise."""
    if self._cascade_ctx is not None:
        self._cascade_ctx.cancel()
```

In `_llm_iterate`:

```python
async def _llm_iterate(self, prompt: str) -> None:
    sink = self._ctx.sink_resolver()
    parser = ToolStreamParser(voice_mode=True)
    chunker = SentenceChunker()
    self._cascade_ctx = CascadeCtx()
    try:
        async for chunk in self._llm.stream_completion(
            system="",
            user=prompt,
            prefill="",
            max_tokens=2048,
            grammar=self._grammar,
            purpose="cascade",
        ):
            if self._cascade_ctx.cancelled:
                logger.info("cascade cancelled mid-stream; exiting cleanly")
                return
            if chunk.text:
                for event in parser.feed(chunk.text):
                    if self._cascade_ctx.cancelled:
                        logger.info("cascade cancelled before event dispatch; exiting")
                        return
                    await self._handle_stream_event(event, sink, chunker)
            if chunk.done:
                break

        if self._cascade_ctx.cancelled:
            return

        for event in parser.flush():
            if self._cascade_ctx.cancelled:
                return
            await self._handle_stream_event(event, sink, chunker)

        if not self._cascade_ctx.cancelled:
            self._flush_chunker(chunker, sink)
    finally:
        self._cascade_ctx = None
```

In `iterate()` — wrap the per-iter call to check cancel state and skip if cancelled (we already exit cleanly, but the next iter starts a new CascadeCtx so this is naturally handled).

- [ ] **Step 4: Pass**

```
uv run pytest tests/test_mind_loop.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/mind_loop.py tests/test_mind_loop.py
git commit -m "feat(mind_loop): cancel propagation via CascadeCtx"
```

---

## Task 3: VoiceSession.abort_speak()

**Files:**
- Modify: `src/dollos/voice/session.py`
- Extend: `tests/test_voice_session_speak_queue.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_voice_session_speak_queue.py — append

@pytest.mark.asyncio
async def test_abort_speak_drains_queue_and_cancels_current():
    """Abort cancels the active synth + drops the rest of the queue."""
    from dollos.voice.session import VoiceSession

    tts = _FakeTTS(delay_s=0.5)  # synth takes 500ms — long enough to interrupt
    session = VoiceSession.__new__(VoiceSession)
    session._tts = tts
    session._outbound_track = None
    session._is_open = True
    session._speak_queue = asyncio.Queue()
    session._speak_worker_task = None

    await session.enqueue_speak("first")
    await session.enqueue_speak("second")
    await session.enqueue_speak("third")
    # Let the worker start the first synth
    await asyncio.sleep(0.1)

    await session.abort_speak()

    # After abort, queue should be empty and no further calls happen
    await asyncio.sleep(0.6)  # would-be time for queued items to drain if not aborted
    assert tts.calls == ["first"]  # only the first was started
    assert session._speak_queue.qsize() == 0
    # Worker task is either done or cancelled
    assert session._speak_worker_task is None or session._speak_worker_task.done()


@pytest.mark.asyncio
async def test_abort_speak_when_idle_is_noop():
    from dollos.voice.session import VoiceSession
    session = VoiceSession.__new__(VoiceSession)
    session._tts = _FakeTTS()
    session._outbound_track = None
    session._is_open = True
    session._speak_queue = asyncio.Queue()
    session._speak_worker_task = None
    # No worker started yet
    await session.abort_speak()  # should not raise


@pytest.mark.asyncio
async def test_speak_after_abort_works():
    """After abort, a new enqueue_speak resumes normally."""
    from dollos.voice.session import VoiceSession
    tts = _FakeTTS(delay_s=0.05)
    session = VoiceSession.__new__(VoiceSession)
    session._tts = tts
    session._outbound_track = None
    session._is_open = True
    session._speak_queue = asyncio.Queue()
    session._speak_worker_task = None

    await session.enqueue_speak("one")
    await asyncio.sleep(0.02)
    await session.abort_speak()

    await session.enqueue_speak("after")
    await session.wait_speak_idle(timeout_s=2.0)
    assert "after" in tts.calls
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

In `src/dollos/voice/session.py`, add:

```python
async def abort_speak(self) -> None:
    """Cancel current TTS task + drain queued texts. Worker becomes dormant;
    next enqueue_speak lazy-starts a fresh worker.

    Capped wait_for on task cancellation: if a TTS engine doesn't honor
    CancelledError within 2s (e.g. GPU call), we move on and clear state.
    """
    task = self._speak_worker_task
    if task is not None and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            logger.exception("speak worker raised during abort")
    self._speak_worker_task = None

    # Drain remaining queue
    while not self._speak_queue.empty():
        try:
            self._speak_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
```

Also: the worker loop's `await self.speak(text)` — speak iterates `self._tts.synthesize(text)`. When the worker task is cancelled, `CancelledError` propagates through that iter. TTS engines should handle it (close their async generator). Verify each TTS engine's `synthesize()` is cancel-safe (typically they are; the async iter just stops).

- [ ] **Step 4: Pass**

- [ ] **Step 5: Commit**

```bash
git add src/dollos/voice/session.py tests/test_voice_session_speak_queue.py
git commit -m "feat(voice): VoiceSession.abort_speak — cancel current + drain queue"
```

---

## Task 4: IPC Interrupt + SayAborted messages

**Files:**
- Modify: `src/dollos/ipc/messages.py`
- Modify: `tests/test_ipc_messages.py` (if exists; otherwise add)

- [ ] **Step 1: Failing test**

```python
# tests/test_ipc_messages.py — append (or create)

def test_decode_interrupt():
    from dollos.ipc.messages import decode_client_message, Interrupt
    msg = decode_client_message('{"type": "interrupt"}')
    assert isinstance(msg, Interrupt)


def test_say_aborted_serializes():
    from dollos.ipc.messages import SayAborted
    m = SayAborted()
    assert m.type == "say_aborted"
    assert m.model_dump()["type"] == "say_aborted"
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

In `src/dollos/ipc/messages.py`:

```python
# Client → server: add Interrupt
class Interrupt(BaseModel):
    type: Literal["interrupt"] = "interrupt"


# Update union:
ClientMessage = Annotated[
    TextInput | Interrupt | WebRTCOfferIn | ICECandidateIn | UtteranceStart | UtteranceEnd,
    Field(discriminator="type"),
]


# Server → client: add SayAborted
class SayAborted(BaseModel):
    type: Literal["say_aborted"] = "say_aborted"
    reason: str = "user_interrupted"


# Update server union:
ServerMessage = Annotated[
    TextChunk | TurnEnd | ErrorMsg | SayAborted | WebRTCAnswerOut | ICECandidateOut,
    Field(discriminator="type"),
]
```

- [ ] **Step 4: Pass**

- [ ] **Step 5: Commit**

```bash
git add src/dollos/ipc/messages.py tests/test_ipc_messages.py
git commit -m "feat(ipc): Interrupt client + SayAborted server messages"
```

---

## Task 5: Kernel auto-interrupt on TextInput; explicit Interrupt handler

**Files:**
- Modify: `src/dollos/kernel.py`
- Extend: `tests/test_kernel.py` (if practical — kernel tests are tricky; could add to integration test instead)

### Pre-flight: IPC pump SayAborted compatibility

Before implementing, verify `src/dollos/ipc/server.py` (or wherever the per-connection sink pump lives) forwards arbitrary `ServerMessage` types — not just `TextChunk` / `TurnEnd`. Look for the function that pulls from sink and writes to WS:

```bash
grep -n "ws.send\|sink.get\|isinstance.*TextChunk" src/dollos/ipc/server.py
```

If the pump branches on specific message types (and would drop a `SayAborted`), update it to forward any `BaseModel` via `model_dump_json()` before continuing.

If the pump already does generic `await ws.send(msg.model_dump_json())`, no change needed.

- [ ] **Step 1: Failing test**

If a unit-test approach is too invasive, skip directly to integration testing via Task 7 smoke. Otherwise:

```python
# Sketch — actual test boilerplate depends on test_kernel.py fixtures
@pytest.mark.asyncio
async def test_text_input_mid_cascade_triggers_cancel_and_abort():
    """Mock mind_loop + voice_session; send TextInput; assert both cancel and abort were called."""
    ...
```

- [ ] **Step 2: Implement**

In `src/dollos/kernel.py` `_handle_message`:

```python
async def _handle_message(self, msg, sink) -> None:
    if isinstance(msg, TextInput):
        # If a cascade is currently active, preempt it before pushing
        # the new input. Cancel cascade + abort any speaking, then send
        # SayAborted IPC so client clears any visual cue, then enqueue
        # the Interrupted perception + new UserSpoke.
        await self._maybe_preempt_for_new_input(sink)
        self._perception_queue.put(
            Perception(
                kind="UserSpoke",
                t=time.time(),
                data={"text": msg.text},
            )
        )
    elif isinstance(msg, Interrupt):
        # Explicit user "stop" without new input. Same preempt + signal but
        # no new UserSpoke perception.
        await self._maybe_preempt_for_new_input(sink)
    elif isinstance(msg, WebRTCOfferIn):
        ...
    ...

async def _maybe_preempt_for_new_input(self, sink) -> None:
    """If a cascade is in flight, cancel it + abort speak + push Interrupted perception."""
    if not self._mind_loop.is_cascade_active:
        return  # idle, nothing to preempt

    # 1. Cancel cascade
    self._mind_loop.cancel_current_cascade()

    # 2. Abort speak
    session = self._voice_sessions.get(id(sink))
    if session is not None:
        await session.abort_speak()

    # 3. Signal client
    sink.put_nowait(SayAborted(reason="user_interrupted"))

    # 4. Push Interrupted perception so Doll knows
    self._perception_queue.put(
        Perception(
            kind="Interrupted",
            t=time.time(),
            data={"by": "user_text_input"},
        )
    )
```

- [ ] **Step 3: Pass tests**

```
uv run pytest --ignore=tests/voice -q
```

Green (any new kernel test added passes; existing tests unaffected since preempt is no-op when cascade is idle).

- [ ] **Step 4: Commit**

```bash
git add src/dollos/kernel.py tests/test_kernel.py
git commit -m "feat(kernel): auto-preempt cascade + abort_speak on new TextInput / Interrupt"
```

---

## Task 6: Interrupted perception rendering + scaffolding

**Files:**
- Modify: `src/dollos/mind/mind_prompt.py`
- Modify: `src/dollos/prompts/templates/scaffolding.jinja`
- Extend: `tests/test_mind_prompt.py`, `tests/test_prompt_renderer.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_mind_prompt.py — append

def test_interrupted_perception_rendered():
    import time
    from dollos.mind.mind_state import MindState, Perception
    from dollos.mind.mind_prompt import _percep_body

    p = Perception(kind="Interrupted", t=time.time(), data={"by": "user_text_input"})
    body = _percep_body(p)
    assert "interrupt" in body.lower() or "cut short" in body.lower()


# tests/test_prompt_renderer.py — append

def test_scaffolding_has_interrupt_section():
    from dollos.prompts.renderer import PromptRenderer
    out = PromptRenderer().render("scaffolding")
    assert "interrupt" in out.lower()
```

(Adapt the render call to whatever pattern the existing tests use.)

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement mind_prompt rendering**

In `src/dollos/mind/mind_prompt.py` `_percep_body`:

```python
if p.kind == "Interrupted":
    by = p.data.get("by", "user")
    return f"your previous turn was cut short by {by}"
```

In scaffolding.jinja, add a short section (after the # Voice / # Format / # Tools section, before # Memory):

```jinja
# Interrupts

If your previous turn was cut short you'll see an `Interrupted` perception.
That means your earlier Say or cascade was aborted. Process the new user
input first; don't try to resume the old line of thought unless the user
asks for it.
```

- [ ] **Step 4: Pass**

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/mind_prompt.py src/dollos/prompts/templates/scaffolding.jinja tests/test_mind_prompt.py tests/test_prompt_renderer.py
git commit -m "feat(perception): Interrupted perception + scaffolding guidance"
```

---

## Task 7: E2E interrupt smoke

**Files:**
- Create: `scripts/smoke_interrupt.py`

- [ ] **Step 1: Write smoke**

Scenario:
1. Boot DollOS
2. Send a prompt that triggers a long-ish response: `"用六句話介紹自己。"`
3. After 1.5s, send another prompt: `"算了, 今天台北幾度?"`
4. Observe:
   - First turn's text_chunks stop arriving (sink should send SayAborted)
   - Second turn proceeds normally, mentions weather

```python
"""smoke_interrupt.py — verify Say cancel + cascade preempt on new user input."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dollos.config import (
    CharacterConfig, DataConfig, IPCConfig, LLMConfig, LogConfig,
    MemsearchConfig, Settings,
)
from dollos.kernel import DollOS


def _make_settings(tmp: Path) -> Settings:
    return Settings(
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://127.0.0.1:8001",
            model_alias="unsloth/Qwen3.6",
            timeout_s=120.0,
        ),
        ipc=IPCConfig(host="127.0.0.1", port=8769),
        log=LogConfig(level="INFO"),
        data=DataConfig(root=tmp / "data"),
        memsearch=MemsearchConfig(top_k=5),
        character=CharacterConfig(
            pack=str(REPO_ROOT / "character_packs" / "gura")
        ),
    )


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="dollos_int_") as tmp:
        dollos = DollOS(_make_settings(Path(tmp)))
        run_task = asyncio.create_task(dollos.run())
        await asyncio.sleep(3.0)

        chunks_before_interrupt: list[str] = []
        say_aborted = False
        chunks_after_interrupt: list[str] = []
        interrupt_sent_at: float = 0.0

        try:
            async with websockets.connect("ws://127.0.0.1:8769") as ws:
                # First prompt — long response expected
                await ws.send(json.dumps({"type": "text_input", "text": "用六句話介紹自己。"}))
                print(f"\n→ sent long prompt", flush=True)

                # Wait until we've received at least one text_chunk (proves streaming
                # has started), then collect ~500ms more, then interrupt.
                first_chunk_seen = False
                end = time.monotonic() + 30.0
                grace_until = None
                while time.monotonic() < end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                    except asyncio.TimeoutError:
                        if grace_until and time.monotonic() >= grace_until:
                            break
                        continue
                    msg = json.loads(raw)
                    if msg.get("type") == "text_chunk":
                        chunks_before_interrupt.append(msg["text"])
                        print(f"  pre: {msg['text']!r}", flush=True)
                        if not first_chunk_seen:
                            first_chunk_seen = True
                            grace_until = time.monotonic() + 0.5
                if not first_chunk_seen:
                    print("WARNING: no text_chunk before timeout; interrupt may not test anything")
                    return 1

                # Interrupt with new input
                print(f"\n→ INTERRUPT with new prompt", flush=True)
                interrupt_sent_at = time.monotonic()
                await ws.send(json.dumps({"type": "text_input", "text": "算了, 今天台北幾度?"}))

                # Drain everything after interrupt for ~30s
                end = time.monotonic() + 30.0
                while time.monotonic() < end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except asyncio.TimeoutError:
                        continue
                    msg = json.loads(raw)
                    if msg.get("type") == "say_aborted":
                        say_aborted = True
                        print("  [say_aborted received]", flush=True)
                    elif msg.get("type") == "text_chunk":
                        chunks_after_interrupt.append(msg["text"])
                        print(f"  post: {msg['text']!r}", flush=True)
                    elif msg.get("type") == "turn_end":
                        print("  [turn_end]", flush=True)
                        break
                    else:
                        print(f"  [{msg.get('type')}]", flush=True)

        finally:
            dollos._mind_loop.shutdown()
            await asyncio.gather(run_task, return_exceptions=True)

    print("\n" + "=" * 60)
    print("OBSERVATIONS")
    print("=" * 60)
    print(f"  text_chunks before interrupt: {len(chunks_before_interrupt)}")
    print(f"  say_aborted received: {say_aborted}")
    print(f"  text_chunks after interrupt: {len(chunks_after_interrupt)}")
    print()
    print(f"  pre-interrupt text: {''.join(chunks_before_interrupt)[:200]!r}")
    print(f"  post-interrupt text: {''.join(chunks_after_interrupt)[:300]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: Run**

```bash
curl -s http://localhost:8001/health || (echo "start llama-server first"; exit 1)
cd /home/progcat/Projects/DollOS/.worktrees/interrupt
uv run python -u scripts/smoke_interrupt.py 2>&1 | tee /tmp/smoke_interrupt_out.log
```

- [ ] **Step 3: Verify**

- `say_aborted` was received? (Yes = abort signal made it through)
- Pre-interrupt chunks << what Doll would have emitted for a 6-sentence intro? (Yes = cascade was cut short)
- Post-interrupt chunks include weather-related content? (Yes = second turn processed)

If yes: smoke pass. If something is wrong, report DONE_WITH_CONCERNS.

- [ ] **Step 4: Commit**

```bash
git add scripts/smoke_interrupt.py
git commit -m "test(voice): interrupt E2E smoke"
```

---

## Self-Review Checklist

**Spec coverage:**
- ✅ Say cancel = abort current TTS + drain queue → Task 3 (abort_speak)
- ✅ Cascade preempt = clean exit at checkpoint → Task 2 (cancel checkpoints in _llm_iterate)
- ✅ External actions keep running → not killed (no SIGTERM logic in plan)
- ✅ Auto-interrupt on new TextInput → Task 5
- ✅ Explicit Interrupt IPC → Tasks 4 + 5
- ✅ SayAborted IPC server message → Tasks 4 + 5
- ✅ Interrupted perception → Tasks 5 + 6
- ✅ Scaffolding guidance → Task 6
- ✅ E2E smoke → Task 7

**Type consistency:**
- `CascadeCtx` consistent throughout
- `cancel_current_cascade()` method name same across all sites
- `abort_speak()` method name consistent
- `SayAborted` / `Interrupt` IPC type strings — "say_aborted" / "interrupt" Pydantic Literals

**Out of scope, documented in plan header:**
- HTTP-level stream cancellation (we exit consumer; server response continues, gets discarded)
- Audio buffer fade-out (best-effort, ~100ms gap acceptable)
- Shell/Subagent process kill (let them finish; results land later)

---

**Plan complete.**
