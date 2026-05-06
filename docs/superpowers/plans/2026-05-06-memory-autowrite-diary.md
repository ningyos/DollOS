# Memory Auto-write + Diary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ephemeral transcript auto-write (raw `[HH:MM user]` / `[HH:MM doll]` bullets indexed for same-day recall, separate from LT memory) plus Doll-written daily diary (scheduler-fired `DiaryEvent` + new `WriteDiary` tool writing to `data/memory/shared/{date}.md`).

**Architecture:** New `dollos.memory_writer.append_transcript` helper writes to `data/memory/transcripts/{date}.md` and triggers `memsearch.index_file`. Dispatcher writes user text in `_handle` finally (after turn) to avoid same-turn recall self-match; `Say.run()` writes Doll text in-tool. New `WriteDiary` pydantic tool writes a `## 日記 (HH:MM)` markdown section. New `DiaryEvent` RawEvent type carries internal-drain sink; `_perceive` builds a "write today's diary" perception; `_sink_of` accepts both UserTextEvent and DiaryEvent. New kernel `_diary_scheduler` background task fires at 23:00 daily; new `_drain_diary_sink` consumes the diary event's sink quietly. `build_memsearch` adds `transcripts_path` so transcripts feed RECALL.

**Tech Stack:** Python 3.12+, `asyncio`, `datetime`, `pydantic`, existing `memsearch`, `pytest` + `pytest-asyncio`. No new external deps.

**Spec:** `docs/superpowers/specs/2026-05-06-memory-autowrite-diary-design.md`

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `src/dollos/memory_writer.py` | new | `append_transcript()` helper |
| `src/dollos/tools.py` | modify | `ToolCtx` adds `transcripts_root` field; `Say.run` appends transcript; new `WriteDiary` class; `TOOLS` list adds `WriteDiary` |
| `src/dollos/events.py` | modify | New `DiaryEvent` RawEvent dataclass |
| `src/dollos/dispatcher.py` | modify | ctor adds `transcripts_root`; `_handle` writes user text in finally; `_perceive` handles DiaryEvent; `_sink_of` accepts DiaryEvent |
| `src/dollos/kernel.py` | modify | `build_memsearch` adds transcripts path; `DollOS.__init__` passes `transcripts_root`; `run()` starts/cancels scheduler; new `_diary_scheduler` and `_drain_diary_sink` methods |
| `tests/test_memory_writer.py` | new | append_transcript unit tests |
| `tests/test_tools.py` | extend | Say transcript write + WriteDiary |
| `tests/test_events.py` | extend | DiaryEvent shape |
| `tests/test_dispatcher.py` | extend | user-text finally write + DiaryEvent path |
| `tests/test_kernel.py` | extend | Scheduler tests |
| `docs/roadmap.md` | modify | Mark step 8 merged |
| `CLAUDE.md` | modify | Same |

---

## Task 1: `dollos/memory_writer.py` — append_transcript helper

**Files:**
- Create: `src/dollos/memory_writer.py`
- Create: `tests/test_memory_writer.py`

Pure helper. No dispatcher / tool / kernel touch.

### Step 1: Write failing tests (RED)

- [ ] Create `tests/test_memory_writer.py`:

```python
"""Tests for memory_writer helpers."""

import asyncio
from datetime import date
from pathlib import Path

import pytest

from dollos.memory_writer import append_transcript


class _FakeMemSearch:
    def __init__(self) -> None:
        self.indexed: list[Path] = []

    async def index_file(self, path):
        self.indexed.append(Path(path))


@pytest.mark.asyncio
async def test_append_transcript_writes_role_tagged_bullet(tmp_path):
    ms = _FakeMemSearch()
    await append_transcript(
        transcripts_root=tmp_path,
        memsearch=ms,
        role="user",
        text="hello",
    )
    expected = tmp_path / f"{date.today():%Y-%m-%d}.md"
    assert expected.exists()
    content = expected.read_text()
    # Format: "- [HH:MM user] hello\n"
    assert content.startswith("- [")
    assert "user] hello\n" in content


@pytest.mark.asyncio
async def test_append_transcript_appends_multiple(tmp_path):
    ms = _FakeMemSearch()
    await append_transcript(
        transcripts_root=tmp_path, memsearch=ms,
        role="user", text="hi",
    )
    await append_transcript(
        transcripts_root=tmp_path, memsearch=ms,
        role="doll", text="hello",
    )
    expected = tmp_path / f"{date.today():%Y-%m-%d}.md"
    content = expected.read_text()
    lines = [ln for ln in content.split("\n") if ln]
    assert len(lines) == 2
    assert "user] hi" in lines[0]
    assert "doll] hello" in lines[1]


@pytest.mark.asyncio
async def test_append_transcript_calls_index_file(tmp_path):
    ms = _FakeMemSearch()
    await append_transcript(
        transcripts_root=tmp_path, memsearch=ms,
        role="user", text="x",
    )
    expected = tmp_path / f"{date.today():%Y-%m-%d}.md"
    assert ms.indexed == [expected]


@pytest.mark.asyncio
async def test_append_transcript_creates_parent_dir(tmp_path):
    ms = _FakeMemSearch()
    nested = tmp_path / "deep" / "transcripts"
    await append_transcript(
        transcripts_root=nested, memsearch=ms,
        role="user", text="x",
    )
    assert (nested / f"{date.today():%Y-%m-%d}.md").exists()
```

- [ ] Run: `uv run pytest tests/test_memory_writer.py -q`
- [ ] Expected: ImportError — `dollos.memory_writer` doesn't exist.

### Step 2: Implement (GREEN)

- [ ] Create `src/dollos/memory_writer.py`:

```python
"""Memory file writers — transcript and diary helpers.

These helpers append role-tagged turn lines to the daily transcript
markdown and trigger memsearch index_file. Used by:
  - EventDispatcher (user turn) → role="user"
  - Say.run() (Doll turn)        → role="doll"

Transcripts are ephemeral and indexed for same-day recall; they live in
data/memory/transcripts/{date}.md (a separate path from shared LT memory).
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memsearch import MemSearch


async def append_transcript(
    *,
    transcripts_root: Path,
    memsearch: MemSearch,
    role: str,
    text: str,
) -> None:
    """Append a turn line to today's transcript and reindex.

    Format per line: `- [HH:MM <role>] <text>\\n`. role is typically
    "user" or "doll". Caller is responsible for ensuring transcripts_root
    is a directory dedicated to transcripts (separate from shared LT memory).
    """
    path = transcripts_root / f"{date.today():%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%H:%M")
    line = f"- [{timestamp} {role}] {text}\n"
    with path.open("a") as f:
        f.write(line)
    await memsearch.index_file(path)
```

### Step 3: Run tests (GREEN)

- [ ] Run: `uv run pytest tests/test_memory_writer.py -q`
- [ ] Expected: 4 passed.

### Step 4: Lint

- [ ] Run: `uv run ruff check src/dollos/memory_writer.py tests/test_memory_writer.py`
- [ ] Expected: clean.

### Step 5: Commit

- [ ] Run:

```bash
git add src/dollos/memory_writer.py tests/test_memory_writer.py
git commit -m "$(cat <<'EOF'
feat(memory_writer): append_transcript helper

Pure async helper that appends a role-tagged turn line ([HH:MM role] X)
to today's transcript markdown and triggers memsearch.index_file.
Used by dispatcher (user) and Say tool (doll). No callers wired yet.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Wire transcripts_root through ToolCtx + Say + Dispatcher + Kernel + memsearch

**Files:**
- Modify: `src/dollos/tools.py`
- Modify: `src/dollos/dispatcher.py`
- Modify: `src/dollos/kernel.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_dispatcher.py`
- Modify: `tests/test_kernel_factories.py` (if it has memsearch tests)

The whole "transcripts_root" plumbing lands together. After this task, both `Say.run` and `_handle`'s finally write transcripts. `build_memsearch` indexes both shared and transcripts dirs.

### Step 1: Write failing tests (RED)

- [ ] Append to `tests/test_tools.py`:

```python
@pytest.mark.asyncio
async def test_say_run_also_appends_to_transcript(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    transcripts_root = tmp_path / "transcripts"
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=transcripts_root,
    )
    await Say(text="hello").run(ctx)

    msg = sink.get_nowait()
    assert isinstance(msg, TextChunk) and msg.text == "hello"

    expected = transcripts_root / f"{date.today():%Y-%m-%d}.md"
    assert expected.exists()
    content = expected.read_text()
    assert "doll] hello" in content
    assert ms.indexed and Path(ms.indexed[-1]) == expected
```

(If existing `_make_ctx` helper in `test_tools.py` doesn't take `transcripts_root`, update it to default `tmp_path / "transcripts"`. Update existing Say tests if they break.)

- [ ] Append to `tests/test_dispatcher.py`:

```python
@pytest.mark.asyncio
async def test_dispatcher_writes_user_text_transcript_after_turn(tmp_path: Path):
    """User text is written to transcript in finally, after the turn completes."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    transcripts_root = tmp_path / "transcripts"
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), character_profile="x",
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=transcripts_root,
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    expected = transcripts_root / f"{date.today():%Y-%m-%d}.md"
    assert expected.exists()
    content = expected.read_text()
    assert "user] hi" in content
    # Both user (from dispatcher) and doll (from Say) get written
    assert "doll] ok" in content
```

(Add to imports in test_dispatcher.py if needed: `from datetime import date`. Update existing dispatcher tests' `EventDispatcher(...)` constructions to pass `transcripts_root=tmp_path / "transcripts"`. Existing `_make_dispatcher` helper if present should grow this kwarg.)

- [ ] Run: `uv run pytest tests/test_tools.py tests/test_dispatcher.py -q`
- [ ] Expected: failures (ToolCtx missing field; EventDispatcher missing kwarg; existing tests break on missing kwarg).

### Step 2: Implement (GREEN) — tools.py

- [ ] Edit `src/dollos/tools.py`. Add `transcripts_root` to `ToolCtx`:

```python
@dataclass
class ToolCtx:
    """Narrow execution context passed to Tool.run()."""

    sink: asyncio.Queue[ServerMessage | None]
    memory_root: Path
    memsearch: MemSearch
    transcripts_root: Path
```

- [ ] Update `Say.run` to also write transcript:

```python
class Say(BaseModel):
    """Stream text to the user. Call this whenever Doll wants to speak."""

    text: str = Field(description="What Doll says to the user.")

    async def run(self, ctx: ToolCtx) -> None:
        ctx.sink.put_nowait(TextChunk(text=self.text))
        try:
            await append_transcript(
                transcripts_root=ctx.transcripts_root,
                memsearch=ctx.memsearch,
                role="doll",
                text=self.text,
            )
        except Exception:
            logger.exception("transcript append failed for Say")
```

- [ ] Add to top of `src/dollos/tools.py` imports:

```python
import logging

from dollos.memory_writer import append_transcript

logger = logging.getLogger(__name__)
```

### Step 3: Implement (GREEN) — dispatcher.py

- [ ] Edit `src/dollos/dispatcher.py`. Update `EventDispatcher.__init__`:

```python
def __init__(
    self,
    *,
    adapter: LLMAdapter,
    inner_voice: InnerVoice,
    instinct: Instinct,
    renderer: PromptRenderer,
    character_profile: str,
    memory_root: Path,
    memsearch: MemSearch,
    transcripts_root: Path,
) -> None:
    self._adapter = adapter
    self._inner_voice = inner_voice
    self._instinct = instinct
    self._renderer = renderer
    self._character_profile = character_profile
    self._memory_root = memory_root
    self._memsearch = memsearch
    self._transcripts_root = transcripts_root
    self._tools_by_name: dict[str, type] = {
        cls.__name__: cls for cls in TOOLS
    }
    self._tasks: set[asyncio.Task[None]] = set()
    self._stopping = False
```

- [ ] Update `_handle` to write user transcript in finally:

```python
async def _handle(self, raw: RawEvent) -> None:
    try:
        sink = self._sink_of(raw)
    except TypeError:
        logger.exception("no sink for raw event %r", type(raw).__name__)
        return

    try:
        doll_event = await self._perceive(raw)
        summary = await self._instinct.process(doll_event)
        await self._respond(doll_event, summary, sink)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("dispatcher _handle error")
        sink.put_nowait(ErrorMsg(message=f"handler error: {e}"))
    finally:
        # Write user text AFTER the turn completes — avoids same-turn
        # recall self-matching (memsearch returning the just-written
        # user message as a hit on its own perception query).
        if isinstance(raw, UserTextEvent):
            try:
                await append_transcript(
                    transcripts_root=self._transcripts_root,
                    memsearch=self._memsearch,
                    role="user",
                    text=raw.text,
                )
            except Exception:
                logger.exception("transcript append failed for UserTextEvent")
        sink.put_nowait(None)
```

- [ ] Update `_respond` to build `ToolCtx` with `transcripts_root`:

```python
ctx = ToolCtx(
    sink=sink,
    memory_root=self._memory_root,
    memsearch=self._memsearch,
    transcripts_root=self._transcripts_root,
)
```

- [ ] Add import to dispatcher.py: `from dollos.memory_writer import append_transcript`

### Step 4: Implement (GREEN) — kernel.py

- [ ] Edit `src/dollos/kernel.py`. Update `build_memsearch`:

```python
def build_memsearch(settings: Settings) -> MemSearch:
    """Construct memsearch rooted at data.root / memory / shared and transcripts."""
    shared_path = settings.data.root / "memory" / "shared"
    transcripts_path = settings.data.root / "memory" / "transcripts"
    shared_path.mkdir(parents=True, exist_ok=True)
    transcripts_path.mkdir(parents=True, exist_ok=True)
    return MemSearch(
        paths=[str(shared_path), str(transcripts_path)],
        embedding_provider="onnx",
    )
```

- [ ] Update `DollOS.__init__` to pass `transcripts_root`:

```python
self.dispatcher = EventDispatcher(
    adapter=self.adapter,
    inner_voice=self.inner_voice,
    instinct=self.instinct,
    renderer=self.renderer,
    character_profile=self._character_profile,
    memory_root=settings.data.root / "memory",
    memsearch=self.memsearch,
    transcripts_root=settings.data.root / "memory" / "transcripts",
)
```

### Step 5: Run tests

- [ ] Run: `uv run pytest -q`
- [ ] Expected: tests pass. Some existing tests in test_tools.py / test_dispatcher.py / test_kernel.py / test_e2e.py may need updating to pass `transcripts_root=tmp_path / "transcripts"` to `ToolCtx(...)` / `EventDispatcher(...)`. Update them — the change is mechanical.

### Step 6: Lint

- [ ] Run: `uv run ruff check src/dollos tests`
- [ ] Expected: clean (modulo any pre-existing issues unrelated to this task).

### Step 7: Commit

- [ ] Run:

```bash
git add src/dollos/tools.py src/dollos/dispatcher.py src/dollos/kernel.py \
        tests/test_tools.py tests/test_dispatcher.py tests/test_e2e.py \
        tests/test_kernel.py tests/test_kernel_factories.py
git commit -m "$(cat <<'EOF'
feat: wire transcripts_root through ToolCtx, Dispatcher, Kernel; auto-write transcripts

ToolCtx + EventDispatcher gain `transcripts_root: Path`. Say.run appends
"- [HH:MM doll] <text>" to today's transcript and reindexes. Dispatcher
_handle writes "- [HH:MM user] <text>" in finally (after turn — avoids
same-turn recall self-match). build_memsearch indexes both shared/ and
transcripts/ paths. Transcript append failures are non-fatal (log only).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `WriteDiary` tool

**Files:**
- Modify: `src/dollos/tools.py`
- Modify: `tests/test_tools.py`

Add a new tool that writes a markdown section to the daily LT file.

### Step 1: Write failing test (RED)

- [ ] Append to `tests/test_tools.py`:

```python
from dollos.tools import WriteDiary


@pytest.mark.asyncio
async def test_write_diary_writes_markdown_section_and_indexes(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    diary = WriteDiary(content="今天我學會了 transcript 跟 diary。")
    await diary.run(ctx)

    expected = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
    assert expected.exists()
    content = expected.read_text()
    assert "## 日記 (" in content
    assert "今天我學會了 transcript 跟 diary。" in content
    assert ms.indexed and Path(ms.indexed[-1]) == expected


def test_write_diary_schema_has_content_field():
    schema = WriteDiary.model_json_schema()
    assert "content" in schema["properties"]
    assert schema["properties"]["content"]["type"] == "string"


def test_write_diary_in_tools_list():
    from dollos.tools import TOOLS
    assert WriteDiary in TOOLS
```

- [ ] Run: `uv run pytest tests/test_tools.py -q`
- [ ] Expected: 3 new tests fail (`WriteDiary` doesn't exist).

### Step 2: Implement (GREEN)

- [ ] Edit `src/dollos/tools.py`. Add after `NoteMemory`:

```python
class WriteDiary(BaseModel):
    """Write today's diary entry to long-term memory.

    Use this once per day when prompted by the diary trigger. The diary
    is a first-person prose narrative reflecting on the day's events AND
    your emotional state. It becomes part of long-term memory and you
    will recall it on future days.
    """

    content: str = Field(
        description=(
            "First-person prose. Cover what happened + how you felt. "
            "Anywhere from a few sentences to a few paragraphs."
        )
    )

    async def run(self, ctx: ToolCtx) -> None:
        path = ctx.memory_root / "shared" / f"{date.today():%Y-%m-%d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%H:%M")
        with path.open("a") as f:
            f.write(f"\n## 日記 ({timestamp})\n\n{self.content}\n")
        await ctx.memsearch.index_file(path)
```

- [ ] Update `TOOLS`:

```python
TOOLS: list[type[BaseModel]] = [Say, NoteMemory, WriteDiary]
```

- [ ] Add `from datetime import datetime` to imports if not already there.

### Step 3: Run tests

- [ ] Run: `uv run pytest tests/test_tools.py -q`
- [ ] Expected: all pass.

### Step 4: Lint

- [ ] Run: `uv run ruff check src/dollos/tools.py tests/test_tools.py`
- [ ] Expected: clean.

### Step 5: Commit

- [ ] Run:

```bash
git add src/dollos/tools.py tests/test_tools.py
git commit -m "$(cat <<'EOF'
feat(tools): WriteDiary pydantic tool

Tool writes a markdown section "## 日記 (HH:MM)\\n\\n<content>\\n" to
today's data/memory/shared/{date}.md and triggers memsearch index_file.
Format intentionally distinct from NoteMemory bullets — diary is a
prose reflection, NoteMemory is a fact bullet.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `DiaryEvent` + dispatcher routing

**Files:**
- Modify: `src/dollos/events.py`
- Modify: `src/dollos/dispatcher.py`
- Modify: `tests/test_events.py`
- Modify: `tests/test_dispatcher.py`

Add the new RawEvent type and wire dispatcher's `_perceive` and `_sink_of`.

### Step 1: Write failing tests (RED)

- [ ] Append to `tests/test_events.py`:

```python
from dollos.events import DiaryEvent


def test_diary_event_is_raw_event_subclass():
    sink: asyncio.Queue = asyncio.Queue()
    evt = DiaryEvent(response_sink=sink)
    from dollos.events import RawEvent
    assert isinstance(evt, RawEvent)


def test_diary_event_holds_response_sink():
    sink: asyncio.Queue = asyncio.Queue()
    evt = DiaryEvent(response_sink=sink)
    assert evt.response_sink is sink
```

- [ ] Append to `tests/test_dispatcher.py`:

```python
@pytest.mark.asyncio
async def test_dispatcher_handles_diary_event(tmp_path: Path):
    """DiaryEvent flows through perceive/respond pipeline; sink eventually
    drains; perception tells Doll to write diary."""

    captured_user_message: list[str] = []

    class _CaptureAdapter:
        def __init__(self):
            self.calls = []

        async def stream_completion(self, **kw):
            self.calls.append(kw)
            captured_user_message.append(kw["user"])
            yield StreamChunk(
                text=(
                    '<tool_call>{"name":"WriteDiary","arguments":'
                    '{"content":"today felt good"}}</tool_call>'
                ),
                done=False,
            )
            yield StreamChunk(text="", done=True)

    from dollos.events import DiaryEvent
    adapter = _CaptureAdapter()
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    transcripts_root = tmp_path / "transcripts"
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), character_profile="x",
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=transcripts_root,
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(DiaryEvent(response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    # The perception told Doll to write a diary
    assert "日記" in captured_user_message[0]
    # WriteDiary tool was actually called → daily file has diary section
    daily_file = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
    assert daily_file.exists()
    assert "## 日記 (" in daily_file.read_text()
```

- [ ] Run: `uv run pytest tests/test_events.py tests/test_dispatcher.py -q`
- [ ] Expected: failures (`DiaryEvent` doesn't exist; dispatcher rejects it).

### Step 2: Implement (GREEN) — events.py

- [ ] Edit `src/dollos/events.py`. Append after `UserTextEvent`:

```python
@dataclass
class DiaryEvent(RawEvent):
    """Scheduled trigger for Doll to write today's diary.

    Has no user-facing sink — the daemon drains internally. Dispatcher's
    _perceive synthesizes a "write today's diary" perception so Doll wakes
    and calls the WriteDiary tool.
    """

    response_sink: asyncio.Queue[ServerMessage | None]
```

### Step 3: Implement (GREEN) — dispatcher.py

- [ ] Edit `src/dollos/dispatcher.py`. Add to imports:

```python
from dollos.events import DollEvent, DiaryEvent, RawEvent, UserTextEvent
```

- [ ] Update `_perceive`:

```python
async def _perceive(self, raw: RawEvent) -> DollEvent:
    if isinstance(raw, UserTextEvent):
        return DollEvent(perception=raw.text, raw=raw)
    if isinstance(raw, DiaryEvent):
        perception = (
            "今天該寫日記了。回顧今天發生的事跟你的感受，"
            "用 WriteDiary tool 寫一段反思。誠實寫，不需要表演。"
        )
        return DollEvent(perception=perception, raw=raw)
    raise TypeError(f"no stub perceive for {type(raw).__name__}")
```

- [ ] Update `_sink_of`:

```python
@staticmethod
def _sink_of(raw: RawEvent) -> asyncio.Queue[ServerMessage | None]:
    if isinstance(raw, (UserTextEvent, DiaryEvent)):
        return raw.response_sink
    raise TypeError(f"no sink for {type(raw).__name__}")
```

### Step 4: Run tests

- [ ] Run: `uv run pytest -q`
- [ ] Expected: all pass.

### Step 5: Lint

- [ ] Run: `uv run ruff check src/dollos/events.py src/dollos/dispatcher.py tests/test_events.py tests/test_dispatcher.py`
- [ ] Expected: clean.

### Step 6: Commit

- [ ] Run:

```bash
git add src/dollos/events.py src/dollos/dispatcher.py tests/test_events.py tests/test_dispatcher.py
git commit -m "$(cat <<'EOF'
feat(events,dispatcher): DiaryEvent + perceive/_sink_of routing

DiaryEvent is a RawEvent with response_sink (drained internally by daemon).
_perceive synthesizes a "write today's diary" natural-language perception.
_sink_of accepts both UserTextEvent and DiaryEvent. End-to-end test
confirms big model (faked) emits WriteDiary tool_call and the daily
markdown ends up with a "## 日記 (HH:MM)" section.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Scheduler + drain task in Kernel

**Files:**
- Modify: `src/dollos/kernel.py`
- Modify: `tests/test_kernel.py`

Add `_diary_scheduler` background task fired daily at 23:00, plus `_drain_diary_sink` consumer. Wire both into `run()` lifecycle with proper shutdown.

### Step 1: Write failing test (RED)

- [ ] Append to `tests/test_kernel.py`:

```python
import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from dollos.events import DiaryEvent
from dollos.ipc.messages import ErrorMsg, TextChunk, TurnEnd


@pytest.mark.asyncio
async def test_drain_diary_sink_consumes_until_sentinel(tmp_path, monkeypatch):
    """_drain_diary_sink eats messages and returns on None sentinel."""
    settings = _make_settings(tmp_path)  # reuse existing helper
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()
    sink.put_nowait(TextChunk(text="ignored"))
    sink.put_nowait(ErrorMsg(message="logged"))
    sink.put_nowait(TurnEnd())
    sink.put_nowait(None)
    # Should return without blocking
    await asyncio.wait_for(dollos._drain_diary_sink(sink), timeout=1.0)


@pytest.mark.asyncio
async def test_diary_scheduler_returns_on_shutdown(tmp_path):
    """Scheduler returns immediately when shutdown is set, even if next fire is far away."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    # Move next fire to far future and signal shutdown immediately
    async def _quickshutdown():
        await asyncio.sleep(0.05)
        dollos._shutdown.set()
    asyncio.create_task(_quickshutdown())
    await asyncio.wait_for(dollos._diary_scheduler(), timeout=2.0)


@pytest.mark.asyncio
async def test_diary_scheduler_dispatches_diary_event_when_fire_time_reached(tmp_path, monkeypatch):
    """When sleep elapses, scheduler dispatches a DiaryEvent and starts a drain task."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)

    dispatched: list = []

    def _capture_dispatch(raw):
        dispatched.append(raw)
        # Send sentinel so drain task can finish
        raw.response_sink.put_nowait(None)

    monkeypatch.setattr(dollos.dispatcher, "dispatch", _capture_dispatch)

    # Make the scheduler fire after a tiny delay by patching now()/replace target
    real_dt = datetime
    fake_now = real_dt(2026, 5, 6, 22, 59, 50)  # 10s before 23:00
    class _FakeDT:
        @classmethod
        def now(cls):
            return fake_now

    monkeypatch.setattr("dollos.kernel.datetime", _FakeDT)
    # Tighten DIARY_HOUR/MINUTE so target == 22:59:50 + 10s == 23:00:00
    # actually fake_now alone is enough: target = fake_now.replace(hour=23, minute=0, ...) = 22:59:50 + 10s = 23:00:00

    async def _delayed_shutdown():
        await asyncio.sleep(0.5)  # let scheduler dispatch
        dollos._shutdown.set()
    asyncio.create_task(_delayed_shutdown())

    # Run scheduler — it should sleep ~10 (real seconds, capped by shutdown) then dispatch
    # but sleep duration is 10s and shutdown fires at 0.5s → no dispatch
    # Solution: monkey patch asyncio.wait_for to fast-forward
    # ... if this gets too tricky, simplify: fire DiaryEvent directly via dispatcher.dispatch
    # and just verify drain task chains correctly. Skip this end-to-end test if too fragile.
```

(If this end-to-end scheduler test gets too fragile due to timing, simplify: only assert `_drain_diary_sink` and `_diary_scheduler shuts down on event`. The scheduler-fires test can be skipped or replaced with a manual `dispatcher.dispatch(DiaryEvent(...))` test that verifies a drain task properly consumes the sink. Pragmatic choice: keep first two tests, drop the third if needed.)

- [ ] Run: `uv run pytest tests/test_kernel.py -q`
- [ ] Expected: failures (no `_diary_scheduler` / `_drain_diary_sink` methods).

### Step 2: Implement (GREEN)

- [ ] Edit `src/dollos/kernel.py`. Add to imports:

```python
from datetime import datetime, timedelta

from dollos.events import DiaryEvent
from dollos.ipc.messages import ErrorMsg
```

- [ ] Add constants and methods to `DollOS` class:

```python
class DollOS:
    DIARY_HOUR = 23   # 23:00 fires (1h buffer before midnight; see spec §12.3)
    DIARY_MINUTE = 0

    def __init__(self, settings: Settings) -> None:
        # ... existing init ...
        self._scheduler_task: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()

    async def _diary_scheduler(self) -> None:
        """Background task: fires DiaryEvent daily at DIARY_HOUR:DIARY_MINUTE."""
        while not self._shutdown.is_set():
            now = datetime.now()
            target = now.replace(
                hour=self.DIARY_HOUR, minute=self.DIARY_MINUTE,
                second=0, microsecond=0,
            )
            if target <= now:
                target = target + timedelta(days=1)
            sleep_s = (target - now).total_seconds()
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=sleep_s
                )
                return  # shutdown signaled
            except asyncio.TimeoutError:
                pass  # time to fire
            sink: asyncio.Queue[ServerMessage | None] = asyncio.Queue()
            asyncio.create_task(self._drain_diary_sink(sink))
            self.dispatcher.dispatch(DiaryEvent(response_sink=sink))

    async def _drain_diary_sink(
        self, sink: asyncio.Queue[ServerMessage | None]
    ) -> None:
        """Consume diary event sink to None sentinel; logs ErrorMsg only."""
        while True:
            item = await sink.get()
            if item is None:
                return
            if isinstance(item, ErrorMsg):
                logger.error("diary event error: %s", item.message)
            # TextChunk / TurnEnd silently consumed
```

- [ ] Update `run()` to start/cancel scheduler:

```python
async def run(self) -> None:
    await self.memsearch.index()
    try:
        await self.server.start()
        self._scheduler_task = asyncio.create_task(self._diary_scheduler())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown.set)
        try:
            await self._shutdown.wait()
        finally:
            await self.server.stop()
            if self._scheduler_task is not None:
                self._scheduler_task.cancel()
                await asyncio.gather(
                    self._scheduler_task, return_exceptions=True
                )
            await self.dispatcher.stop()
    finally:
        pass
```

### Step 3: Run tests

- [ ] Run: `uv run pytest tests/test_kernel.py -q`
- [ ] Expected: kernel tests pass. (If the third scheduler test from Step 1 was too fragile, drop it — keep only `test_drain_diary_sink_consumes_until_sentinel` and `test_diary_scheduler_returns_on_shutdown`.)
- [ ] Run: `uv run pytest -q`
- [ ] Expected: full suite green.

### Step 4: Lint

- [ ] Run: `uv run ruff check src/dollos/kernel.py tests/test_kernel.py`
- [ ] Expected: clean.

### Step 5: Commit

- [ ] Run:

```bash
git add src/dollos/kernel.py tests/test_kernel.py
git commit -m "$(cat <<'EOF'
feat(kernel): _diary_scheduler + _drain_diary_sink for daily DiaryEvent

DollOS gains a background asyncio task that sleeps until 23:00 each day,
dispatches a DiaryEvent with an internal-drain sink, and loops. Drain task
consumes the sink quietly (errors logged, content discarded). Scheduler
shuts down cleanly via _shutdown event before dispatcher.stop() so any
in-flight diary turn can finish.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Manual smoke test

**Files:** None (validation only)

Verify daemon, transcript, and diary work against real models.

### Step 1: Verify servers running

- [ ] `curl -s -o /dev/null -w "8001=%{http_code} 8003=%{http_code}\n" http://localhost:8001/health http://localhost:8003/health`
- [ ] Expected: both `200`.

### Step 2: Start daemon

- [ ] `cd /home/progcat/Projects/DollOS/.worktrees/memory-autowrite-diary`
- [ ] `rm -f /tmp/dollos.log`
- [ ] `mkdir -p data/memory/shared data/memory/transcripts`
- [ ] `uv run python -m dollos --config config.toml > /tmp/dollos.log 2>&1 &`
- [ ] `sleep 6 && tail -3 /tmp/dollos.log`
- [ ] Expected: "memsearch indexed" + "ipc server listening".

### Step 3: Drive a normal turn (validates transcript auto-write)

- [ ] `uv run python experiments/ws_client.py "你好"`
- [ ] Expected: clean Doll response.
- [ ] `cat data/memory/transcripts/$(date +%Y-%m-%d).md`
- [ ] Expected: two lines — `- [HH:MM user] 你好` and `- [HH:MM doll] <Doll's response>`.

### Step 4: Force diary (validate diary path)

To avoid waiting until 23:00, trigger DiaryEvent manually:

- [ ] Stop daemon: `pkill -f "python -m dollos"`
- [ ] Run a one-shot Python script to dispatch DiaryEvent through the running daemon's dispatcher. Easiest: temporarily set `DIARY_HOUR` to current hour and `DIARY_MINUTE` to a minute or two ahead, restart daemon, and wait. OR: write a small test script `experiments/fire_diary.py`:

```python
import asyncio
import json
import websockets

async def main():
    # No WS protocol path for DiaryEvent — for smoke we'd need to add a
    # one-off CLI hack to kernel, OR just observe the natural 23:00 fire.
    # Pragmatic alternative: bump DIARY_MINUTE to current+2 in code,
    # restart daemon, wait 2 minutes.
    print("To smoke-test diary: edit DIARY_HOUR/MINUTE in kernel.py to "
          "current+2 minutes; restart daemon; wait; observe daily.md")

asyncio.run(main())
```

- [ ] Pragmatic approach: in `src/dollos/kernel.py` temporarily change `DIARY_HOUR/MINUTE` to fire ~2 minutes after daemon start, run smoke, then **revert before commit**.

- [ ] Restart daemon, drive 2-3 conversational turns, then wait until DIARY time.

- [ ] After scheduled time:
  - [ ] `cat data/memory/shared/$(date +%Y-%m-%d).md`
  - [ ] Expected: contains `## 日記 (HH:MM)` heading + Doll's prose reflection on the turns.
- [ ] `cat data/memory/transcripts/$(date +%Y-%m-%d).md`
  - [ ] Expected: still contains all turn lines (transcript unchanged by diary).

### Step 5: Verify diary appears in next-turn recall

- [ ] Drive one more turn: `uv run python experiments/ws_client.py "你還記得我們今天聊了什麼嗎"`
- [ ] Expected: Doll references content from her diary entry.

### Step 6: Stop daemon, revert temporary timing change

- [ ] `pkill -f "python -m dollos"`
- [ ] Revert `DIARY_HOUR/MINUTE` back to `23:00` in `src/dollos/kernel.py`
- [ ] `git diff src/dollos/kernel.py` — confirm no unintended changes remain.

### Step 7: Document outcomes

- [ ] If transcript captures cleanly + diary writes + recall hits diary → Task 6 done.
- [ ] If model doesn't call WriteDiary on DiaryEvent (model behavior issue) → record observation; spec §12.4 already acknowledges. Don't block merge.

No commit needed unless smoke test reveals a real bug.

---

## Task 7: Roadmap + CLAUDE.md sync

**Files:**
- Modify: `docs/roadmap.md`
- Modify: `CLAUDE.md`

### Step 1: Update `docs/roadmap.md`

- [ ] In `## 已完成` table, append:

```markdown
| Roadmap step 8 — Memory auto-write + Diary | Merged |
```

- [ ] In `### 8. Memory（自動寫）` heading, replace body with:

```
**Re-cut**: roadmap 原文「v1 寫全部、無顯著性過濾」採折衷——transcript 走 ephemeral 路徑（同日 recall 可見），LT memory 由 Doll 自己寫日記產生。

Step 8 minimal scope: `memory_writer.append_transcript` 寫 `[HH:MM role] X` 到 `data/memory/transcripts/{date}.md`（dispatcher 在 `_handle` finally 寫 user，Say.run 寫 doll）。memsearch 索引兩個目錄。新 `WriteDiary` pydantic tool 寫 markdown section 到 `data/memory/shared/{date}.md`。新 `DiaryEvent` RawEvent + dispatcher routing；kernel `_diary_scheduler` 每日 23:00 fire；`_drain_diary_sink` 內部消費。情緒走大模型 think 自由發揮，無新 emotion infrastructure。

**Demo**：對話自動進 transcript（即時可 recall），每日固定時間 Doll 醒來寫日記（含情緒），隔日 recall 引用日記反思。
```

### Step 2: Update `CLAUDE.md`

- [ ] In "已完成" plan table, append:

```markdown
| Roadmap step 8 — Memory auto-write + Diary | Merged |
```

- [ ] Replace "下一個" paragraph with step-9 brief:

```
**Roadmap step 9 — Subagent**：spawn_subagent tool（external、fast=False）。Inline definition + 隔離 session + 預算（max_tokens / max_wall_clock_s）。fast=False async pattern：execute 立即回 dispatched-ack，subagent 跑完自己 push SubagentResultEvent 回 queue（真正用上 step 4 spec 預告的 RawEvent queue routing）。**並行**：reflex 仍待 research+brainstorm。完整 roadmap：`docs/roadmap.md`。
```

### Step 3: Verify

- [ ] `uv run pytest -q` — green.
- [ ] `uv run ruff check src/dollos tests` — clean.

### Step 4: Commit

- [ ] Run:

```bash
git add docs/roadmap.md CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: mark roadmap step 8 (Memory auto-write + Diary) merged, point to step 9

Step 8 re-cut: transcript ephemeral auto-write + Doll-written diary
replaces roadmap-original "raw auto-write everything" anti-pattern,
informed by long-term-memory frameworks research.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Done definition

- [ ] All 7 tasks committed on branch `memory-autowrite-diary`.
- [ ] `uv run pytest -q` green.
- [ ] `uv run ruff check src/dollos tests` clean.
- [ ] Smoke test (Task 6): transcript + diary + cross-turn recall confirmed.
- [ ] Roadmap + CLAUDE.md updated.
- [ ] Ready for `superpowers:finishing-a-development-branch`.
