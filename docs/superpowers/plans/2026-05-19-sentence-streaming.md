# Plan 2: Sentence Streaming + Audio Serialization

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Doll's voice stream out sentence-by-sentence with low first-audio latency, and serialize the TTS playback queue so multiple speak segments in one turn never overlap. Lifts the Plan 1 scaffolding restriction "prefer single trailing speak segment".

**Architecture:** Two interlocking changes.
1. `SentenceChunker` splits the SpeakChunk stream from the cascade parser at sentence boundaries (`. ? ! 。 ？ ！ \n\n` + forced flush after N chars). Each sentence becomes its own `TextChunk` to IPC.
2. `VoiceSession` learns a per-session serialized speak queue + worker so even if 10 `TextChunk` messages arrive within 100ms, TTS synth runs strictly FIFO — never concurrent.

**Tech Stack:** Python 3.13, asyncio, the existing `dollos.voice` TTS engines (fish-tts / qwen3-tts / piper / luxtts).

**Out of scope (Plan 3):**
- Interrupt / cancel-current-speak

---

## File Structure

**New files:**
- `src/dollos/cascade/__init__.py` — package marker for the new cascade subpackage (currently `cascade.py` is a top-level module; coexists fine)
- `src/dollos/cascade/sentence_chunker.py` — `SentenceChunker` class
- `tests/test_sentence_chunker.py`
- `tests/test_voice_session_speak_queue.py`
- `scripts/smoke_sentence_streaming.py`

**Modified:**
- `src/dollos/mind/mind_loop.py` — instantiate `SentenceChunker()` per iter; pipe `SpeakChunk.text` through it; emit one `TextChunk(sentence)` per chunker output
- `src/dollos/voice/session.py` — add `speak_queue: asyncio.Queue[str]` + persistent worker task; expose `enqueue_speak(text)` instead of direct `speak(text)`
- `src/dollos/voice/sink.py` — `TTSObservingSink.put_nowait` calls `session.enqueue_speak(text)` instead of `asyncio.create_task(session.speak(...))`
- `src/dollos/prompts/templates/scaffolding.jinja` — drop the temporary "single trailing speak block" restriction (no longer needed)

---

## Task 1: SentenceChunker

**Files:**
- Create: `src/dollos/cascade/__init__.py` (empty package marker, with a one-line docstring)
- Create: `src/dollos/cascade/sentence_chunker.py`
- Create: `tests/test_sentence_chunker.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_sentence_chunker.py
from dollos.cascade.sentence_chunker import SentenceChunker


def test_splits_on_english_punct():
    c = SentenceChunker()
    out = list(c.feed("Hello there. ")) + list(c.feed("How are you? ")) + list(c.feed("Fine"))
    out += list(c.flush())
    assert out == ["Hello there. ", "How are you? ", "Fine"]


def test_splits_on_chinese_punct():
    c = SentenceChunker()
    out = list(c.feed("你好。今天好嗎？")) + list(c.flush())
    assert out == ["你好。", "今天好嗎？"]


def test_splits_on_exclamation_and_newline():
    c = SentenceChunker()
    out = list(c.feed("Stop!\nWait")) + list(c.flush())
    assert out == ["Stop!\n", "Wait"]


def test_split_across_feeds():
    c = SentenceChunker()
    out = []
    out += list(c.feed("Hello th"))
    out += list(c.feed("ere. Bye"))
    out += list(c.flush())
    assert out == ["Hello there. ", "Bye"]


def test_forced_flush_on_max_chars():
    c = SentenceChunker(max_chars=10)
    out = list(c.feed("abcdefghijklmnop")) + list(c.flush())
    assert out[0] == "abcdefghij"
    assert "".join(out) == "abcdefghijklmnop"


def test_empty_input():
    c = SentenceChunker()
    assert list(c.feed("")) == []
    assert list(c.flush()) == []


def test_trailing_whitespace_after_punct_included():
    """The chunker should consume trailing spaces after punctuation."""
    c = SentenceChunker()
    out = list(c.feed("Hi.  Bye")) + list(c.flush())
    assert out == ["Hi.  ", "Bye"]
```

- [ ] **Step 2: Run, expect failure** (module not present)

```
uv run pytest tests/test_sentence_chunker.py -v
```

- [ ] **Step 3: Implement**

```python
# src/dollos/cascade/sentence_chunker.py
"""Sentence-boundary chunker for TTS-friendly streaming text.

Buffers tokens from the LLM stream until a sentence-ending punctuation
(or newline pair) is seen, then emits the sentence as one chunk.
Forced-flush after `max_chars` so very long unpunctuated runs don't
delay audio indefinitely.

Used by mind_loop between the parser's SpeakChunk events and the
IPC TextChunk emit, so each TextChunk corresponds to a TTS-friendly
unit.
"""
from __future__ import annotations

_PUNCT = {".", "?", "!", "。", "？", "！", "\n"}


class SentenceChunker:
    def __init__(self, max_chars: int = 120) -> None:
        self._buf = ""
        self._max = max_chars

    def feed(self, text: str) -> list[str]:
        self._buf += text
        out: list[str] = []
        # Find punct boundaries left-to-right
        i = 0
        while i < len(self._buf):
            if self._buf[i] in _PUNCT:
                end = i + 1
                # Consume trailing whitespace (but not newlines — newline IS a boundary char itself)
                while end < len(self._buf) and self._buf[end] == " ":
                    end += 1
                out.append(self._buf[:end])
                self._buf = self._buf[end:]
                i = 0
                continue
            i += 1
        # Forced flush: emit max_chars windows if buf grew past threshold without punct
        while len(self._buf) >= self._max:
            out.append(self._buf[:self._max])
            self._buf = self._buf[self._max:]
        return out

    def flush(self) -> list[str]:
        if self._buf:
            tail = self._buf
            self._buf = ""
            return [tail]
        return []
```

- [ ] **Step 4: Pass**

- [ ] **Step 5: Commit**

```bash
git add src/dollos/cascade/__init__.py src/dollos/cascade/sentence_chunker.py tests/test_sentence_chunker.py
git commit -m "feat(cascade): SentenceChunker for TTS-friendly streaming text"
```

---

## Task 2: VoiceSession serialized speak queue

**Files:**
- Modify: `src/dollos/voice/session.py`
- Create: `tests/test_voice_session_speak_queue.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_voice_session_speak_queue.py
import asyncio
import pytest


class _FakeTTS:
    """Records each synthesize call + sleeps to simulate work."""
    sample_rate = 16000

    def __init__(self, delay_s: float = 0.1) -> None:
        self.calls: list[str] = []
        self.in_flight: list[str] = []
        self.max_concurrent: int = 0
        self._delay_s = delay_s

    async def synthesize(self, text: str):
        # mark start
        self.calls.append(text)
        self.in_flight.append(text)
        self.max_concurrent = max(self.max_concurrent, len(self.in_flight))
        await asyncio.sleep(self._delay_s)
        # yield empty PCM
        yield b""
        self.in_flight.remove(text)

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_speak_queue_serializes():
    """Enqueueing multiple texts in rapid succession runs them strictly FIFO, not parallel."""
    from dollos.voice.session import VoiceSession

    tts = _FakeTTS(delay_s=0.05)
    session = VoiceSession.__new__(VoiceSession)  # bypass __init__ since we're not wiring full WebRTC
    # Manually set the minimum fields needed for the queue/worker
    session._tts = tts
    session._outbound_track = None  # _push_outbound short-circuits when None
    session._is_open = True
    await session._start_speak_worker()  # new method (see implementation step)

    try:
        await session.enqueue_speak("one")
        await session.enqueue_speak("two")
        await session.enqueue_speak("three")
        # Wait for queue to drain
        await session.wait_speak_idle(timeout_s=2.0)
    finally:
        await session._stop_speak_worker()

    assert tts.calls == ["one", "two", "three"]
    assert tts.max_concurrent == 1  # serialized: never more than one in flight at a time


@pytest.mark.asyncio
async def test_enqueue_speak_returns_immediately():
    """enqueue_speak doesn't block on TTS; returns ~instantly."""
    import time
    from dollos.voice.session import VoiceSession

    tts = _FakeTTS(delay_s=0.5)  # synth takes 500ms
    session = VoiceSession.__new__(VoiceSession)
    session._tts = tts
    session._outbound_track = None
    session._is_open = True
    await session._start_speak_worker()

    try:
        t0 = time.monotonic()
        await session.enqueue_speak("hello")
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, f"enqueue should return instantly, took {elapsed:.3f}s"
    finally:
        await session._stop_speak_worker()
```

- [ ] **Step 2: Run, expect failure** (`enqueue_speak` / `_start_speak_worker` not present)

- [ ] **Step 3: Implement**

Edit `src/dollos/voice/session.py`. In `VoiceSession.__init__`, add:

```python
self._speak_queue: asyncio.Queue[str | None] = asyncio.Queue()
self._speak_worker_task: asyncio.Task | None = None
```

Add methods:

```python
async def _start_speak_worker(self) -> None:
    """Launch the serialized speak worker. Idempotent."""
    if self._speak_worker_task is None or self._speak_worker_task.done():
        self._speak_worker_task = asyncio.create_task(self._speak_worker_loop())

async def _stop_speak_worker(self) -> None:
    """Signal worker shutdown and wait for it to exit."""
    if self._speak_worker_task is not None and not self._speak_worker_task.done():
        await self._speak_queue.put(None)  # sentinel
        try:
            await asyncio.wait_for(self._speak_worker_task, timeout=5.0)
        except asyncio.TimeoutError:
            self._speak_worker_task.cancel()

async def _speak_worker_loop(self) -> None:
    """Pull text from speak_queue and synth one at a time."""
    while True:
        text = await self._speak_queue.get()
        if text is None:
            return
        try:
            await self.speak(text)
        except Exception:
            logger.exception("speak worker error on text=%r", text[:80])

async def enqueue_speak(self, text: str) -> None:
    """Queue text for serialized TTS playback. Non-blocking (returns when enqueued)."""
    await self._speak_queue.put(text)

async def wait_speak_idle(self, timeout_s: float = 30.0) -> None:
    """Wait for the speak queue to be empty AND the worker idle. For tests + tidy shutdown."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while not self._speak_queue.empty():
        await asyncio.sleep(0.02)
        if asyncio.get_event_loop().time() > deadline:
            raise asyncio.TimeoutError("speak queue did not drain")
    # Plus a small grace for the worker to finish the last item
    await asyncio.sleep(0.05)
```

Also: in the existing `speak()` method, **don't change behavior** — it's still the one that does the actual TTS. The worker calls it. External callers should now use `enqueue_speak` instead.

In `VoiceSession.__init__`, call `_start_speak_worker()` (or call it from wherever the session becomes "ready" — verify with existing code; speak worker should be alive while session is open).

In `close()`: call `_stop_speak_worker()` before tearing down TTS.

- [ ] **Step 4: Pass**

```
uv run pytest tests/test_voice_session_speak_queue.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/dollos/voice/session.py tests/test_voice_session_speak_queue.py
git commit -m "feat(voice): VoiceSession serialized speak queue + worker"
```

---

## Task 3: TTSObservingSink uses enqueue_speak

**Files:**
- Modify: `src/dollos/voice/sink.py`
- Modify: `tests/test_tts_observing_sink.py` (if exists; otherwise add basic test)

- [ ] **Step 1: Failing test**

```python
# tests/test_tts_observing_sink.py (or extend existing)
import asyncio
import pytest
from dollos.ipc.messages import TextChunk
from dollos.voice.sink import TTSObservingSink


class _MockSession:
    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.create_task_called = False
    async def enqueue_speak(self, text: str) -> None:
        self.enqueued.append(text)


@pytest.mark.asyncio
async def test_sink_calls_enqueue_speak_not_create_task():
    session = _MockSession()
    sink = TTSObservingSink(voice_session_provider=lambda: session)
    sink.put_nowait(TextChunk(text="hello"))
    sink.put_nowait(TextChunk(text="world"))
    # Let the event loop pick up the scheduled coroutines
    await asyncio.sleep(0.05)
    assert session.enqueued == ["hello", "world"]
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

Edit `src/dollos/voice/sink.py` `put_nowait`:

```python
def put_nowait(self, item: Any) -> None:
    super().put_nowait(item)
    if isinstance(item, TextChunk):
        session = self._voice_session_provider()
        if session is not None:
            try:
                asyncio.get_running_loop()
                # Use enqueue_speak so the per-session worker serializes synth.
                asyncio.create_task(session.enqueue_speak(item.text))
            except RuntimeError:
                logger.warning(
                    "TTSObservingSink: TextChunk put_nowait outside event "
                    "loop; TTS not scheduled (text=%r)", item.text,
                )
            except Exception:
                logger.exception("scheduling enqueue_speak failed")
```

Key change: `session.speak(item.text)` → `session.enqueue_speak(item.text)`. The `asyncio.create_task` wrapper is preserved because `enqueue_speak` is still async (it does `await self._speak_queue.put(text)`, which can in theory block if the queue is bounded — but our queue is unbounded, so it's instant).

- [ ] **Step 4: Pass**

- [ ] **Step 5: Commit**

```bash
git add src/dollos/voice/sink.py tests/test_tts_observing_sink.py
git commit -m "feat(voice): TTSObservingSink routes through enqueue_speak"
```

---

## Task 4: Cascade pipes SpeakChunks through SentenceChunker

**Files:**
- Modify: `src/dollos/mind/mind_loop.py`
- Modify: `tests/test_mind_loop.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_mind_loop.py — append
@pytest.mark.asyncio
async def test_iterate_sentence_chunks_speech_to_sink():
    """Naked LLM text containing multiple sentences should arrive as separate TextChunks."""
    # Mock the adapter to stream:
    #   <think>...\n</think>\n\nHello there. How are you? Fine.
    # Expect 3 TextChunk messages: "Hello there. ", "How are you? ", "Fine."
    ...
```

(Reuse existing mock-LLM boilerplate from `test_mind_loop.py`.)

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

In `mind_loop.py`:

- Add import: `from dollos.cascade.sentence_chunker import SentenceChunker`
- Instantiate `chunker = SentenceChunker()` at the top of `_llm_iterate` (one per iter)
- Modify `_handle_stream_event`:

```python
async def _handle_stream_event(self, event, sink, chunker) -> None:
    if isinstance(event, SpeakChunk):
        if not event.text:
            return
        for sentence in chunker.feed(event.text):
            sink.put_nowait(TextChunk(text=sentence))
            self._state.recent_outputs.append(OutputRecord(
                kind="Speech",
                t=time.time(),
                summary=f"spoke: {sentence[:60]}",
            ))
    elif isinstance(event, ToolCallReady):
        await self._dispatch_tool(event.name, event.arguments)
```

Pass `chunker` through from `_llm_iterate`:

```python
async def _llm_iterate(self, prompt: str) -> None:
    sink = self._ctx.sink_resolver()
    parser = ToolStreamParser(voice_mode=True)
    chunker = SentenceChunker()

    async for chunk in self._llm.stream_completion(...):
        if chunk.text:
            for event in parser.feed(chunk.text):
                await self._handle_stream_event(event, sink, chunker)
        if chunk.done:
            break

    for event in parser.flush():
        await self._handle_stream_event(event, sink, chunker)

    # Flush the chunker tail
    for sentence in chunker.flush():
        sink.put_nowait(TextChunk(text=sentence))
        self._state.recent_outputs.append(OutputRecord(
            kind="Speech",
            t=time.time(),
            summary=f"spoke: {sentence[:60]}",
        ))
```

- [ ] **Step 4: Pass**

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/mind_loop.py tests/test_mind_loop.py
git commit -m "feat(mind_loop): sentence-chunk SpeakChunks before TextChunk emit"
```

---

## Task 5: Drop scaffolding "single trailing speak" restriction

**Files:**
- Modify: `src/dollos/prompts/templates/scaffolding.jinja`
- Modify: `tests/test_prompt_renderer.py`

- [ ] **Step 1: Failing test**

```python
def test_scaffolding_no_longer_restricts_single_speak_block():
    from dollos.prompts.renderer import render_scaffolding
    out = render_scaffolding(
        identity=None, rules=[], examples=[], available_skills=[], tool_registry={},
    )
    # The Plan 1 restriction should be gone now that Plan 2 serializes audio
    assert "single trailing speak" not in out
    assert "single block at the end of your turn" not in out
    assert "Plan 2 fixes it" not in out
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Edit scaffolding**

Remove the "For this milestone, prefer putting your spoken text as a single block at the end of your turn..." paragraph from `scaffolding.jinja`. Keep "You may interleave speak and tool_call segments freely."

- [ ] **Step 4: Pass**

- [ ] **Step 5: Commit**

```bash
git add src/dollos/prompts/templates/scaffolding.jinja tests/test_prompt_renderer.py
git commit -m "tune(prompt): drop Plan-1 single-trailing-speak restriction (Plan 2 serializes audio)"
```

---

## Task 6: E2E smoke + first-audio benchmark

**Files:**
- Create: `scripts/smoke_sentence_streaming.py`

- [ ] **Step 1: Write smoke**

```python
"""Boot DollOS; send a multi-sentence prompt; measure first text_chunk latency."""
# (Pattern mirrors scripts/smoke_voice_first.py)
# After WS connect, send: "用三句話介紹自己。" (asks for multiple sentences)
# Record: time of first text_chunk arrival, total turn time, count of text_chunks
# Verify no audio overlap (smoke can't directly test that — see Task 2's unit test)
```

- [ ] **Step 2: Start llama-server if needed**

```bash
curl -s http://localhost:8001/health || echo "start llama-server first"
```

- [ ] **Step 3: Run**

```bash
uv run python -u scripts/smoke_sentence_streaming.py 2>&1 | tee /tmp/smoke_sentence.log
```

Manually inspect:
- First text_chunk arrives well before turn_end? (E.g. < 3s after prompt sent for a multi-sentence response)
- text_chunks form coherent sentences (not character-by-character)
- Test memory file shows any expected tool_call dispatches

- [ ] **Step 4: Commit smoke**

```bash
git add scripts/smoke_sentence_streaming.py
git commit -m "test(voice): sentence streaming + first-audio latency smoke"
```

---

## Self-Review Checklist

**Spec coverage:**
- ✅ SentenceChunker boundary splitting → Task 1
- ✅ Forced flush at max_chars → Task 1
- ✅ VoiceSession serialized speak queue → Task 2
- ✅ enqueue_speak returns immediately → Task 2
- ✅ Sink routes through enqueue_speak → Task 3
- ✅ Cascade pipes through chunker → Task 4
- ✅ Plan 1 restriction lifted → Task 5
- ✅ E2E smoke + first-audio metric → Task 6

**Type consistency:**
- `SentenceChunker` class consistent; `feed(str) -> list[str]`, `flush() -> list[str]`
- `enqueue_speak(text: str) -> None` consistent
- `_speak_queue: asyncio.Queue[str | None]` with `None` sentinel for shutdown

**Out of scope (Plan 3):**
- Interrupt / abort_speak — separate plan
- `_active_speak_task` cancellation API — Plan 3 adds it on top of this serialized worker

**Known limitations after Plan 2:**
- Each enqueued speak runs to completion. If the user starts speaking mid-Doll-utterance, Doll's voice doesn't stop — the queue keeps draining. Plan 3 fixes this with abort_speak() that cancels current task + drains queue.

---

**Plan complete.**
