# Tool Calling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace direct big-model text streaming with structured `<tool_call>` JSON dispatch. Big model `</think>` 後唯一合法輸出 = `<tool_call>` blocks; dispatcher stream-parses them and routes to pydantic-model tools (`Say`, `NoteMemory`). Tool result NOT cascaded back (single round per event; cascade is step 7).

**Architecture:** New `dollos.tools` defines `Say`/`NoteMemory` as pydantic `BaseModel` subclasses with `run(ctx)` methods + module-level `TOOLS` list + `ToolCtx` dataclass. New `dollos.tool_parser.ToolStreamParser` is a stateless state machine extracting tool_call dicts from streaming text (drops everything outside `<tool_call>...</tool_call>`). `Qwen3ThinkingTemplate.render()` accepts `tools=` and renders a `# Tools` section in system prompt with JSON schemas; `Qwen3PlainTemplate.render(tools=non_empty)` raises. `LLMAdapter.stream_completion` plumbs `tools=` through `ComposedLLMAdapter` to template. `EventDispatcher._respond` becomes parser-driven: feed each stream chunk → for each parsed `tool_call`, validate via `model_validate` and `await tool.run(ctx)`. `EventDispatcher` ctor gains `memory_root: Path` + `memsearch: MemSearch` for `ToolCtx`. Kernel `DollOS.__init__` wires both.

**Tech Stack:** Python 3.12+, `pydantic` v2 (`BaseModel`, `Field`, `ValidationError`, `model_json_schema()`), `asyncio`, `memsearch`, `pytest` + `pytest-asyncio`. No new external deps (pydantic already in tree via memsearch).

**Spec:** `docs/superpowers/specs/2026-05-05-tool-calling-design.md`

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `src/dollos/tools.py` | new | `ToolCtx` dataclass + `Say` + `NoteMemory` pydantic models + `TOOLS` list |
| `src/dollos/tool_parser.py` | new | `ToolStreamParser` state machine |
| `src/dollos/llm/templates.py` | modify | `PromptTemplate.render` ABC adds `tools=`; `Qwen3ThinkingTemplate` renders `# Tools` section; `Qwen3PlainTemplate` raises on non-empty tools |
| `src/dollos/llm/adapter.py` | modify | `LLMAdapter.stream_completion` adds `tools=None` kwarg |
| `src/dollos/llm/composed.py` | modify | `ComposedLLMAdapter.stream_completion` passes `tools=` to template |
| `src/dollos/dispatcher.py` | modify | `_respond` rewritten parser-driven; ctor takes `memory_root` + `memsearch`; new private `_dispatch_tool_call` |
| `src/dollos/kernel.py` | modify | `DollOS.__init__` passes `memory_root` + `memsearch` into dispatcher |
| `tests/test_tools.py` | new | Tool unit tests |
| `tests/test_tool_parser.py` | new | Parser unit tests |
| `tests/test_llm_templates.py` | extend | Template `tools=` rendering tests |
| `tests/test_llm_composed.py` | extend | Adapter `tools=` plumbing test |
| `tests/test_dispatcher.py` | extend | New parser-driven dispatch tests; existing assertions adjusted |
| `tests/test_kernel.py` | modify | Kernel `EventDispatcher` construction passes new kwargs |
| `tests/test_e2e.py` | modify | Update for tool-call-shaped output |

---

## Task 1: `dollos/tools.py` — Tool classes + ToolCtx + TOOLS list

**Files:**
- Create: `src/dollos/tools.py`
- Create: `tests/test_tools.py`

Pure tool definitions. No dispatcher / parser touch.

### Step 1: Write failing tests (RED)

- [ ] Create `tests/test_tools.py`:

```python
"""Tests for Tool classes (Say, NoteMemory) and ToolCtx."""

import asyncio
from datetime import date
from pathlib import Path

import pytest

from dollos.ipc.messages import TextChunk
from dollos.tools import TOOLS, NoteMemory, Say, ToolCtx


class _FakeMemSearch:
    """Fake MemSearch — captures index_file calls."""

    def __init__(self) -> None:
        self.indexed: list[Path] = []

    async def index_file(self, path):
        self.indexed.append(Path(path))


def _make_ctx(tmp_path: Path) -> tuple[ToolCtx, _FakeMemSearch, asyncio.Queue]:
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    ctx = ToolCtx(sink=sink, memory_root=tmp_path, memsearch=ms)
    return ctx, ms, sink


def test_say_schema_has_text_field():
    schema = Say.model_json_schema()
    assert "text" in schema["properties"]
    assert schema["properties"]["text"]["type"] == "string"


def test_note_memory_schema_has_text_field():
    schema = NoteMemory.model_json_schema()
    assert "text" in schema["properties"]


def test_tools_list_contains_both():
    assert Say in TOOLS
    assert NoteMemory in TOOLS


@pytest.mark.asyncio
async def test_say_run_pushes_text_chunk(tmp_path):
    ctx, _ms, sink = _make_ctx(tmp_path)
    say = Say(text="你好")
    await say.run(ctx)

    msg = sink.get_nowait()
    assert isinstance(msg, TextChunk)
    assert msg.text == "你好"


@pytest.mark.asyncio
async def test_note_memory_run_appends_bullet_to_daily_file(tmp_path):
    ctx, _ms, _sink = _make_ctx(tmp_path)
    note = NoteMemory(text="主人喜歡咖啡")
    await note.run(ctx)

    expected_path = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
    assert expected_path.exists()
    content = expected_path.read_text()
    assert content.endswith("- 主人喜歡咖啡\n")


@pytest.mark.asyncio
async def test_note_memory_run_calls_memsearch_index_file(tmp_path):
    ctx, ms, _sink = _make_ctx(tmp_path)
    note = NoteMemory(text="another fact")
    await note.run(ctx)

    expected_path = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
    assert ms.indexed == [expected_path]


@pytest.mark.asyncio
async def test_note_memory_run_appends_to_existing_file(tmp_path):
    ctx, _ms, _sink = _make_ctx(tmp_path)
    expected_path = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
    expected_path.parent.mkdir(parents=True)
    expected_path.write_text("# header\n\n- old fact\n")

    await NoteMemory(text="new fact").run(ctx)

    content = expected_path.read_text()
    assert "old fact" in content
    assert content.endswith("- new fact\n")
```

- [ ] Run: `uv run pytest tests/test_tools.py -q`
- [ ] Expected: ImportError / collection error — `dollos.tools` doesn't exist.

### Step 2: Implement (GREEN)

- [ ] Create `src/dollos/tools.py`:

```python
"""Tool definitions — pydantic models with run() methods.

Step 6 minimal: two tools (Say, NoteMemory). Tool = BaseModel; args are
fields; description = docstring; schema = model_json_schema(); execution
= run(ctx). Single source of truth per tool.

Future: step 7 adds reflex (whitelist via class attribute), step 9 adds
spawn_subagent (fast=False async pattern). For now no permission /
streamable / fast metadata — YAGNI.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from dollos.ipc.messages import ServerMessage, TextChunk

if TYPE_CHECKING:
    from memsearch import MemSearch


@dataclass
class ToolCtx:
    """Narrow execution context passed to Tool.run()."""

    sink: asyncio.Queue[ServerMessage | None]
    memory_root: Path
    memsearch: "MemSearch"


class Say(BaseModel):
    """Stream text to the user. Call this whenever Doll wants to speak."""

    text: str = Field(description="What Doll says to the user.")

    async def run(self, ctx: ToolCtx) -> None:
        ctx.sink.put_nowait(TextChunk(text=self.text))


class NoteMemory(BaseModel):
    """Record a fact into Doll's memory (daily markdown + memsearch index)."""

    text: str = Field(
        description="The fact to record. One sentence, declarative."
    )

    async def run(self, ctx: ToolCtx) -> None:
        path = ctx.memory_root / "shared" / f"{date.today():%Y-%m-%d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Sync append + index inside async — append is a single small write
        # (microseconds). asyncio.to_thread wrap is YAGNI for step 6.
        with path.open("a") as f:
            f.write(f"- {self.text}\n")
        await ctx.memsearch.index_file(path)


TOOLS: list[type[BaseModel]] = [Say, NoteMemory]
```

### Step 3: Run tests (GREEN)

- [ ] Run: `uv run pytest tests/test_tools.py -q`
- [ ] Expected: 7 passed.

### Step 4: Lint

- [ ] Run: `uv run ruff check src/dollos/tools.py tests/test_tools.py`
- [ ] Expected: clean.

### Step 5: Commit

- [ ] Run:

```bash
git add src/dollos/tools.py tests/test_tools.py
git commit -m "$(cat <<'EOF'
feat(tools): Say + NoteMemory pydantic tool models + ToolCtx

Step 6 first piece: tool definitions only (no parser, no dispatcher
wiring). Tool class itself = BaseModel; args = fields; description =
docstring; schema = model_json_schema(); execution = run(ctx). Module-
level TOOLS list. NoteMemory writes daily markdown + calls memsearch
index_file synchronously.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `dollos/tool_parser.py` — ToolStreamParser state machine

**Files:**
- Create: `src/dollos/tool_parser.py`
- Create: `tests/test_tool_parser.py`

Stateless drop-everything-outside-`<tool_call>` state machine. Pure sync (logging only side effect).

### Step 1: Write failing tests (RED)

- [ ] Create `tests/test_tool_parser.py`:

```python
"""Tests for ToolStreamParser."""

import logging

import pytest

from dollos.tool_parser import ToolStreamParser


def test_single_tool_call_in_one_chunk():
    p = ToolStreamParser()
    out = p.feed(
        '<tool_call>\n{"name": "Say", "arguments": {"text": "hi"}}\n</tool_call>'
    )
    assert out == [{"name": "Say", "arguments": {"text": "hi"}}]


def test_two_consecutive_tool_calls():
    p = ToolStreamParser()
    out = p.feed(
        '<tool_call>{"name":"A","arguments":{}}</tool_call>'
        '<tool_call>{"name":"B","arguments":{}}</tool_call>'
    )
    assert out == [
        {"name": "A", "arguments": {}},
        {"name": "B", "arguments": {}},
    ]


def test_tool_call_split_across_chunks():
    p = ToolStreamParser()
    a = p.feed('<tool_call>{"name":"Say",')
    b = p.feed('"arguments":{"text":"x"}}</tool_call>')
    assert a == []
    assert b == [{"name": "Say", "arguments": {"text": "x"}}]


def test_naked_text_outside_tool_call_is_dropped(caplog):
    p = ToolStreamParser()
    with caplog.at_level(logging.DEBUG, logger="dollos.tool_parser"):
        out = p.feed("some thinking\n")
    assert out == []


def test_think_content_is_dropped_just_like_naked_text():
    p = ToolStreamParser()
    out = p.feed("<think>internal reasoning</think>\n")
    assert out == []


def test_naked_text_then_tool_call_extracts_only_tool():
    p = ToolStreamParser()
    out = p.feed(
        'pre-think reasoning\n'
        '<tool_call>{"name":"Say","arguments":{"text":"hi"}}</tool_call>'
    )
    assert out == [{"name": "Say", "arguments": {"text": "hi"}}]


def test_malformed_json_in_tool_call_logs_warning_and_continues(caplog):
    p = ToolStreamParser()
    with caplog.at_level(logging.WARNING, logger="dollos.tool_parser"):
        out = p.feed(
            '<tool_call>{not json}</tool_call>'
            '<tool_call>{"name":"Say","arguments":{"text":"after"}}</tool_call>'
        )
    assert out == [{"name": "Say", "arguments": {"text": "after"}}]
    assert any("malformed" in r.message.lower() or "json" in r.message.lower()
               for r in caplog.records)


def test_unclosed_tool_call_logs_on_flush(caplog):
    p = ToolStreamParser()
    out = p.feed('<tool_call>{"name":"Say"')
    assert out == []
    with caplog.at_level(logging.WARNING, logger="dollos.tool_parser"):
        rest = p.flush()
    assert rest == []
    assert any("unclosed" in r.message.lower() or "unfinished" in r.message.lower()
               for r in caplog.records)


def test_unicode_in_tool_call():
    p = ToolStreamParser()
    out = p.feed(
        '<tool_call>{"name":"Say","arguments":{"text":"你好"}}</tool_call>'
    )
    assert out == [{"name": "Say", "arguments": {"text": "你好"}}]


def test_open_marker_split_across_chunks():
    p = ToolStreamParser()
    a = p.feed("<tool_")
    b = p.feed('call>{"name":"X","arguments":{}}</tool_call>')
    assert a == []
    assert b == [{"name": "X", "arguments": {}}]


def test_close_marker_split_across_chunks():
    p = ToolStreamParser()
    a = p.feed('<tool_call>{"name":"X","arguments":{}}</tool_')
    b = p.feed("call>")
    assert a == []
    assert b == [{"name": "X", "arguments": {}}]


def test_flush_on_clean_state_returns_empty():
    p = ToolStreamParser()
    p.feed('<tool_call>{"name":"X","arguments":{}}</tool_call>')
    assert p.flush() == []
```

- [ ] Run: `uv run pytest tests/test_tool_parser.py -q`
- [ ] Expected: ImportError.

### Step 2: Implement (GREEN)

- [ ] Create `src/dollos/tool_parser.py`:

```python
"""ToolStreamParser — extract <tool_call> JSON blocks from a streaming text.

Stateless drop-everything-outside policy:
  - Text outside <tool_call>...</tool_call> markers is dropped (DEBUG log)
  - Inside markers: accumulate and json.loads on </tool_call>
  - Malformed JSON: WARNING + skip + reset to OUTSIDE
  - Unclosed at flush(): WARNING + drop

Used by EventDispatcher to route big-model output to tool dispatch.
"""

from __future__ import annotations

import json
import logging
from enum import Enum, auto

logger = logging.getLogger(__name__)

OPEN = "<tool_call>"
CLOSE = "</tool_call>"


class _State(Enum):
    OUTSIDE = auto()
    INSIDE = auto()


class ToolStreamParser:
    """State machine: accumulates stream chunks, yields parsed tool_call dicts.

    Caller pattern:
        for chunk in stream:
            for call in parser.feed(chunk):
                dispatch(call)
        for call in parser.flush():
            dispatch(call)
    """

    def __init__(self) -> None:
        self._state = _State.OUTSIDE
        self._buf = ""           # rolling tail; may hold split markers
        self._inside_buf = ""    # accumulated JSON between markers

    def feed(self, chunk: str) -> list[dict]:
        """Process a chunk; return zero or more parsed tool_call dicts."""
        self._buf += chunk
        out: list[dict] = []
        while True:
            if self._state is _State.OUTSIDE:
                idx = self._buf.find(OPEN)
                if idx < 0:
                    # Keep last (len(OPEN)-1) chars in case the marker is
                    # being split across the boundary.
                    keep = len(OPEN) - 1
                    if len(self._buf) > keep:
                        dropped = self._buf[:-keep]
                        self._buf = self._buf[-keep:]
                        if dropped:
                            logger.debug("dropped naked text: %r", dropped)
                    break
                # Found OPEN
                dropped = self._buf[:idx]
                if dropped:
                    logger.debug("dropped naked text: %r", dropped)
                self._buf = self._buf[idx + len(OPEN):]
                self._state = _State.INSIDE
                self._inside_buf = ""
            else:  # INSIDE
                idx = self._buf.find(CLOSE)
                if idx < 0:
                    keep = len(CLOSE) - 1
                    if len(self._buf) > keep:
                        self._inside_buf += self._buf[:-keep]
                        self._buf = self._buf[-keep:]
                    break
                # Found CLOSE
                self._inside_buf += self._buf[:idx]
                self._buf = self._buf[idx + len(CLOSE):]
                payload = self._inside_buf.strip()
                self._inside_buf = ""
                self._state = _State.OUTSIDE
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError as e:
                    logger.warning(
                        "malformed JSON in <tool_call>: %s; payload=%r",
                        e, payload,
                    )
                    continue
                if not isinstance(parsed, dict):
                    logger.warning(
                        "tool_call payload is not a JSON object: %r", parsed
                    )
                    continue
                out.append(parsed)
        return out

    def flush(self) -> list[dict]:
        """Call at stream end. Logs unclosed tool_call if any; returns [].

        Always returns an empty list — flush is for cleanup logging.
        """
        if self._state is _State.INSIDE or self._inside_buf:
            logger.warning(
                "unclosed <tool_call> at stream end; dropped %r",
                self._inside_buf + self._buf,
            )
        elif self._buf:
            logger.debug("trailing naked text at stream end: %r", self._buf)
        self._buf = ""
        self._inside_buf = ""
        self._state = _State.OUTSIDE
        return []
```

### Step 3: Run tests (GREEN)

- [ ] Run: `uv run pytest tests/test_tool_parser.py -q`
- [ ] Expected: 12 passed.

### Step 4: Lint

- [ ] Run: `uv run ruff check src/dollos/tool_parser.py tests/test_tool_parser.py`
- [ ] Expected: clean.

### Step 5: Commit

- [ ] Run:

```bash
git add src/dollos/tool_parser.py tests/test_tool_parser.py
git commit -m "$(cat <<'EOF'
feat(tool_parser): ToolStreamParser — extract <tool_call> JSON from stream

Stateless drop-everything-outside-tool_call policy. Buffers across chunk
boundaries (handles split open/close markers and split JSON). Malformed
JSON → WARNING + skip + reset. Unclosed at flush → WARNING + drop.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Template `tools=` extension

**Files:**
- Modify: `src/dollos/llm/templates.py`
- Modify: `tests/test_llm_templates.py`

`PromptTemplate` ABC adds `tools=None` kwarg. `Qwen3ThinkingTemplate.render()` renders a `# Tools` section in system prompt when `tools` is non-empty. `Qwen3PlainTemplate.render()` raises `NotImplementedError` on non-empty tools.

### Step 1: Inspect existing tests (RED reference)

- [ ] Read `tests/test_llm_templates.py` to find existing test patterns and helpers.

### Step 2: Write failing tests (RED)

- [ ] Append to `tests/test_llm_templates.py`:

```python
import json

import pytest
from pydantic import BaseModel, Field

from dollos.llm.templates import Qwen3PlainTemplate, Qwen3ThinkingTemplate


class _ExampleSay(BaseModel):
    """Stream text to the user."""
    text: str = Field(description="What to say.")


class _ExampleNote(BaseModel):
    """Record a fact."""
    text: str = Field(description="What to record.")


def test_thinking_template_with_tools_renders_tools_block():
    t = Qwen3ThinkingTemplate()
    rendered = t.render(
        system="You are Doll.",
        user="hi",
        prefill="",
        tools=[_ExampleSay, _ExampleNote],
    )
    assert "# Tools" in rendered
    assert "<tools>" in rendered
    assert "</tools>" in rendered
    # Tool names must appear (model needs to know how to call)
    assert "_ExampleSay" in rendered
    assert "_ExampleNote" in rendered
    # Schema must include text field
    assert '"text"' in rendered


def test_thinking_template_without_tools_omits_block():
    t = Qwen3ThinkingTemplate()
    rendered = t.render(system="You are Doll.", user="hi", prefill="")
    assert "# Tools" not in rendered
    assert "<tools>" not in rendered


def test_thinking_template_empty_tools_list_omits_block():
    t = Qwen3ThinkingTemplate()
    rendered = t.render(system="You are Doll.", user="hi", prefill="", tools=[])
    assert "# Tools" not in rendered


def test_thinking_template_tools_block_contains_valid_json():
    t = Qwen3ThinkingTemplate()
    rendered = t.render(
        system="x", user="y", prefill="", tools=[_ExampleSay]
    )
    start = rendered.index("<tools>") + len("<tools>")
    end = rendered.index("</tools>")
    payload = rendered[start:end].strip()
    parsed = json.loads(payload)
    assert isinstance(parsed, list)
    assert parsed[0]["name"] == "_ExampleSay"
    assert "description" in parsed[0]
    assert "parameters" in parsed[0]


def test_plain_template_rejects_non_empty_tools():
    t = Qwen3PlainTemplate()
    with pytest.raises(NotImplementedError):
        t.render(system="x", user="y", prefill="", tools=[_ExampleSay])


def test_plain_template_accepts_none_tools():
    t = Qwen3PlainTemplate()
    out = t.render(system="x", user="y", prefill="", tools=None)
    assert "x" in out


def test_plain_template_accepts_empty_tools_list():
    t = Qwen3PlainTemplate()
    out = t.render(system="x", user="y", prefill="", tools=[])
    assert "x" in out
```

- [ ] Run: `uv run pytest tests/test_llm_templates.py -q`
- [ ] Expected: 7 new tests fail (TypeError: render() got unexpected kwarg 'tools').

### Step 3: Implement (GREEN)

- [ ] Edit `src/dollos/llm/templates.py`. Replace whole file with:

```python
"""PromptTemplate — model-family-specific prompt rendering."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod

from pydantic import BaseModel


class PromptTemplate(ABC):
    """Render a (system, user, prefill) tuple into the single prompt string
    the model expects.

    For "server-applied" templates (e.g. Anthropic / OpenAI chat completions
    where the API takes messages instead of a raw prompt), a concrete
    PromptTemplate may be a no-op stub and the corresponding Provider would
    talk in messages directly. Plan 3 v1 doesn't ship such a Provider, but
    the interface allows it.
    """

    @abstractmethod
    def render(
        self,
        *,
        system: str,
        user: str,
        prefill: str,
        tools: list[type[BaseModel]] | None = None,
    ) -> str:
        ...


def _format_tools_block(tools: list[type[BaseModel]]) -> str:
    """Render the `# Tools` system-prompt section for Qwen3 native tool calling."""
    schemas = [
        {
            "name": cls.__name__,
            "description": (cls.__doc__ or "").strip(),
            "parameters": cls.model_json_schema(),
        }
        for cls in tools
    ]
    schemas_json = json.dumps(schemas, ensure_ascii=False, indent=2)
    return (
        "\n\n# Tools\n\n"
        "You have tools. To call a tool, emit:\n"
        "<tool_call>\n"
        '{"name": "<tool_name>", "arguments": {<args>}}\n'
        "</tool_call>\n\n"
        "After </think>, output ONLY <tool_call> blocks. "
        "Plain text after </think> is invalid.\n\n"
        "Available tools:\n"
        "<tools>\n"
        f"{schemas_json}\n"
        "</tools>"
    )


class Qwen3ThinkingTemplate(PromptTemplate):
    """Qwen3.x thinking-model ChatML.

    Opens the <think> block inside the assistant turn so prefill content
    goes inside the thinking block. Renders an optional `# Tools` section
    in the system prompt for tool calling.
    """

    def render(
        self,
        *,
        system: str,
        user: str,
        prefill: str,
        tools: list[type[BaseModel]] | None = None,
    ) -> str:
        if tools:
            system = system + _format_tools_block(tools)
        parts = [
            "<|im_start|>system",
            system,
            "<|im_end|>",
            "<|im_start|>user",
            user,
            "<|im_end|>",
            "<|im_start|>assistant",
            "<think>",
            "",
        ]
        rendered = "\n".join(parts)
        if prefill:
            rendered += prefill
        return rendered


class Qwen3PlainTemplate(PromptTemplate):
    """Qwen3.x ChatML with thinking immediately closed.

    Inner Voice's small models. Rejects non-empty tools — small-model
    code paths must not attempt tool calling (raises NotImplementedError
    to surface misuse loudly).
    """

    def render(
        self,
        *,
        system: str,
        user: str,
        prefill: str,
        tools: list[type[BaseModel]] | None = None,
    ) -> str:
        if tools:
            raise NotImplementedError(
                "Qwen3PlainTemplate does not support tool calling; "
                "use Qwen3ThinkingTemplate for tool-calling code paths."
            )
        parts = [
            "<|im_start|>system",
            system,
            "<|im_end|>",
            "<|im_start|>user",
            user,
            "<|im_end|>",
            "<|im_start|>assistant",
            "<think>",
            "",
            "</think>",
            "",
            "",
        ]
        rendered = "\n".join(parts)
        if prefill:
            rendered += prefill
        return rendered
```

### Step 4: Run tests (GREEN)

- [ ] Run: `uv run pytest tests/test_llm_templates.py -q`
- [ ] Expected: all template tests (existing + 7 new) pass.

### Step 5: Lint

- [ ] Run: `uv run ruff check src/dollos/llm/templates.py tests/test_llm_templates.py`
- [ ] Expected: clean.

### Step 6: Commit

- [ ] Run:

```bash
git add src/dollos/llm/templates.py tests/test_llm_templates.py
git commit -m "$(cat <<'EOF'
feat(templates): tools= kwarg on PromptTemplate; thinking renders Tools block

Qwen3ThinkingTemplate renders `# Tools` section in system prompt when
tools= is non-empty (Qwen3 native <tools>...</tools> + <tool_call> JSON
format). Qwen3PlainTemplate raises NotImplementedError on non-empty
tools — small-model code paths (Inner Voice) must not request tools.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `LLMAdapter` / `ComposedLLMAdapter` plumb `tools=`

**Files:**
- Modify: `src/dollos/llm/adapter.py`
- Modify: `src/dollos/llm/composed.py`
- Modify: `tests/test_llm_composed.py`

Add `tools=None` kwarg through; ComposedLLMAdapter forwards to template.render. Provider/transport unchanged.

### Step 1: Write failing tests (RED)

- [ ] Append to `tests/test_llm_composed.py`:

```python
from pydantic import BaseModel, Field

from dollos.llm.composed import ComposedLLMAdapter


class _ToolDummy(BaseModel):
    """Dummy tool."""
    text: str = Field(description="x")


@pytest.mark.asyncio
async def test_composed_passes_tools_to_template():
    """ComposedLLMAdapter forwards `tools=` into template.render()."""

    captured = {}

    class _CaptureTemplate:
        def render(self, *, system, user, prefill, tools=None):
            captured["tools"] = tools
            return "RENDERED"

    class _StubProvider:
        async def stream(self, *, prompt, stop=None, max_tokens=1024):
            captured["prompt"] = prompt
            yield StreamChunk(text="ok", done=True)

    adapter = ComposedLLMAdapter(
        provider=_StubProvider(), template=_CaptureTemplate()
    )
    chunks = []
    async for ch in adapter.stream_completion(
        system="s", user="u", prefill="", tools=[_ToolDummy]
    ):
        chunks.append(ch)

    assert captured["tools"] == [_ToolDummy]
    assert captured["prompt"] == "RENDERED"
```

(If `pytest`, `StreamChunk`, `pytest_asyncio` imports are already at file top, skip; otherwise add at top of file:
```python
import pytest
from dollos.llm.adapter import StreamChunk
```)

- [ ] Run: `uv run pytest tests/test_llm_composed.py -q`
- [ ] Expected: TypeError: stream_completion got unexpected kwarg `tools`.

### Step 2: Implement (GREEN)

- [ ] Edit `src/dollos/llm/adapter.py`. Add `tools` to `stream_completion` ABC:

```python
"""Abstract LLM adapter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import BaseModel


@dataclass(frozen=True)
class StreamChunk:
    """A single streamed chunk from the LLM."""

    text: str
    done: bool = False


class LLMAdapter(ABC):
    """Abstract interface for LLM backends.

    All concrete adapters MUST support prefill — assistant-side text that the
    model continues from. This is critical for VoM (see grammar_injection_techreport.md).
    """

    @abstractmethod
    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools: "list[type[BaseModel]] | None" = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a completion. `tools` is forwarded to the template; transports
        ignore it (the prompt encodes tool definitions as text)."""
        ...
```

- [ ] Edit `src/dollos/llm/composed.py`:

```python
"""ComposedLLMAdapter — combines a Provider with a PromptTemplate."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.llm.templates import PromptTemplate
from dollos.llm.transport import Provider

if TYPE_CHECKING:
    from pydantic import BaseModel


class ComposedLLMAdapter(LLMAdapter):
    """Combine a Provider with a PromptTemplate to satisfy LLMAdapter."""

    def __init__(self, provider: Provider, template: PromptTemplate) -> None:
        self._provider = provider
        self._template = template

    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools: "list[type[BaseModel]] | None" = None,
    ) -> AsyncIterator[StreamChunk]:
        prompt = self._template.render(
            system=system, user=user, prefill=prefill, tools=tools
        )
        async for chunk in self._provider.stream(
            prompt=prompt, stop=stop, max_tokens=max_tokens
        ):
            yield chunk
```

### Step 3: Run tests (GREEN)

- [ ] Run: `uv run pytest tests/test_llm_composed.py tests/test_llm_templates.py tests/test_llm_transport.py -q`
- [ ] Expected: all pass.

### Step 4: Lint

- [ ] Run: `uv run ruff check src/dollos/llm/adapter.py src/dollos/llm/composed.py tests/test_llm_composed.py`
- [ ] Expected: clean.

### Step 5: Commit

- [ ] Run:

```bash
git add src/dollos/llm/adapter.py src/dollos/llm/composed.py tests/test_llm_composed.py
git commit -m "$(cat <<'EOF'
feat(llm): plumb tools= through LLMAdapter / ComposedLLMAdapter

ABC and ComposedLLMAdapter accept tools=None kwarg and pass it to
template.render(). Transports unchanged — tool definitions are encoded
as text in the rendered prompt.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `EventDispatcher._respond` — parser-driven tool dispatch

**Files:**
- Modify: `src/dollos/dispatcher.py`
- Modify: `tests/test_dispatcher.py`

Rewrite `_respond` to feed stream into `ToolStreamParser`, dispatch each parsed call to its tool. Add `memory_root` + `memsearch` to ctor for `ToolCtx` building. Add private `_dispatch_tool_call`.

### Step 1: Write failing tests (RED)

- [ ] Open `tests/test_dispatcher.py`. Add a `_FakeMemSearch` helper near other fakes:

```python
class _FakeMemSearch:
    def __init__(self) -> None:
        self.indexed: list = []

    async def index_file(self, path):
        self.indexed.append(path)
```

- [ ] Update `_make_dispatcher` (or every direct `EventDispatcher(...)` construction) to pass `memory_root=tmp_path` (use a pytest `tmp_path` fixture in tests that need it; for tests not using filesystem, pass `Path("/tmp/dollos-test-mem")` or similar — adapt to existing fixture style) and `memsearch=_FakeMemSearch()`.

- [ ] Update existing `_FakeAdapter` chunks in tests so the big-model output is now `<tool_call>...</tool_call>` shaped. Specifically: the existing tests asserting prefill content stay valid (prefill construction unchanged). Existing tests asserting `TextChunk(text="ok")` reaches the sink need their `chunks` updated to:

```python
chunks=[
    StreamChunk(
        text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
        done=False,
    ),
    StreamChunk(text="", done=True),
]
```

- [ ] Add new tests:

```python
import json
from pathlib import Path

@pytest.mark.asyncio
async def test_dispatcher_routes_say_tool_call_to_text_chunk(tmp_path: Path):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"hello"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice(recall_text="RECALL:\n- foo\n")
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=PromptRenderer(),
        character_profile="You are Doll.",
        memory_root=tmp_path,
        memsearch=ms,
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))

    items = []
    while True:
        item = await sink.get()
        if item is None:
            break
        items.append(item)

    text_chunks = [m for m in items if isinstance(m, TextChunk)]
    assert any(m.text == "hello" for m in text_chunks)
    assert any(isinstance(m, TurnEnd) for m in items)


@pytest.mark.asyncio
async def test_dispatcher_routes_note_memory_tool_call(tmp_path: Path):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text=(
                    '<tool_call>{"name":"NoteMemory","arguments":'
                    '{"text":"likes coffee"}}</tool_call>'
                ),
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice(recall_text="RECALL:\n- foo\n")
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=PromptRenderer(),
        character_profile="You are Doll.",
        memory_root=tmp_path,
        memsearch=ms,
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        item = await sink.get()
        if item is None:
            break

    # Find written file
    shared = tmp_path / "shared"
    files = list(shared.glob("*.md"))
    assert len(files) == 1
    assert files[0].read_text().endswith("- likes coffee\n")
    assert ms.indexed and Path(ms.indexed[0]) == files[0]


@pytest.mark.asyncio
async def test_dispatcher_executes_multiple_tool_calls_in_order(tmp_path: Path):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text=(
                    '<tool_call>{"name":"NoteMemory","arguments":'
                    '{"text":"a"}}</tool_call>'
                    '<tool_call>{"name":"Say","arguments":'
                    '{"text":"b"}}</tool_call>'
                ),
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), character_profile="x",
        memory_root=tmp_path, memsearch=ms,
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    items = []
    while True:
        m = await sink.get()
        if m is None:
            break
        items.append(m)

    # NoteMemory wrote, then Say emitted text
    assert (tmp_path / "shared").exists()
    assert any(isinstance(m, TextChunk) and m.text == "b" for m in items)


@pytest.mark.asyncio
async def test_dispatcher_naked_text_is_dropped(tmp_path: Path):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text="leaked thinking text\n", done=False),
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"x"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="trailing leak\n", done=False),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), character_profile="x",
        memory_root=tmp_path, memsearch=ms,
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    items = []
    while True:
        m = await sink.get()
        if m is None:
            break
        items.append(m)

    text_chunks = [m for m in items if isinstance(m, TextChunk)]
    assert len(text_chunks) == 1
    assert text_chunks[0].text == "x"


@pytest.mark.asyncio
async def test_dispatcher_unknown_tool_logs_and_skips(tmp_path: Path, caplog):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"WhoKnows","arguments":{}}</tool_call>',
                done=False,
            ),
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"after"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), character_profile="x",
        memory_root=tmp_path, memsearch=ms,
    )
    sink: asyncio.Queue = asyncio.Queue()
    with caplog.at_level("WARNING"):
        disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
        items = []
        while True:
            m = await sink.get()
            if m is None:
                break
            items.append(m)

    text_chunks = [m for m in items if isinstance(m, TextChunk)]
    assert any(m.text == "after" for m in text_chunks)


@pytest.mark.asyncio
async def test_dispatcher_validation_error_logs_and_skips(tmp_path: Path, caplog):
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"wrong":"k"}}</tool_call>',
                done=False,
            ),
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
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), character_profile="x",
        memory_root=tmp_path, memsearch=ms,
    )
    sink: asyncio.Queue = asyncio.Queue()
    with caplog.at_level("WARNING"):
        disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
        items = []
        while True:
            m = await sink.get()
            if m is None:
                break
            items.append(m)

    text_chunks = [m for m in items if isinstance(m, TextChunk)]
    assert any(m.text == "ok" for m in text_chunks)


@pytest.mark.asyncio
async def test_dispatcher_passes_tools_to_adapter(tmp_path: Path):
    adapter = _FakeAdapter(
        chunks=[StreamChunk(text="", done=True)]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), character_profile="x",
        memory_root=tmp_path, memsearch=ms,
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    assert len(adapter.calls) == 1
    # _FakeAdapter must capture `tools` kwarg — see Step 2 (extending fake).
    from dollos.tools import TOOLS
    assert adapter.calls[0].get("tools") == TOOLS
```

- [ ] Update `_FakeAdapter.stream_completion` to accept and capture `tools=` kwarg:

```python
async def stream_completion(
    self,
    *,
    system: str,
    user: str,
    prefill: str = "",
    stop: list[str] | None = None,
    max_tokens: int = 1024,
    tools: list[type] | None = None,
) -> AsyncIterator[StreamChunk]:
    self.calls.append(
        {"system": system, "user": user, "prefill": prefill, "tools": tools}
    )
    if self.delay:
        await asyncio.sleep(self.delay)
    for c in self.chunks:
        yield c
```

- [ ] Run: `uv run pytest tests/test_dispatcher.py -q`
- [ ] Expected: failures (TypeError on EventDispatcher new kwargs; existing tests using `text="ok"` chunks fail because they're now naked text and dropped).

### Step 2: Implement (GREEN)

- [ ] Edit `src/dollos/dispatcher.py`. Replace whole file:

```python
"""EventDispatcher — fan-out raw events to concurrent tasks.

Step 6: _respond now feeds the big-model stream into ToolStreamParser
and dispatches each parsed tool_call to its pydantic-model tool's run().
The big model emits ONLY <tool_call> blocks after </think>; naked text
is dropped (DEBUG log) by the parser. Single-round per event — tool
results NOT cascaded back (cascade is step 7).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from memsearch import MemSearch
from pydantic import ValidationError

from dollos.events import DollEvent, RawEvent, UserTextEvent
from dollos.inner_voice import InnerVoice
from dollos.instinct import Instinct
from dollos.ipc.messages import ErrorMsg, ServerMessage, TurnEnd
from dollos.llm.adapter import LLMAdapter
from dollos.prompts import PromptRenderer
from dollos.tool_parser import ToolStreamParser
from dollos.tools import TOOLS, ToolCtx

logger = logging.getLogger(__name__)


class EventDispatcher:
    """Spawns one asyncio.Task per RawEvent. No worker, no queue."""

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
    ) -> None:
        self._adapter = adapter
        self._inner_voice = inner_voice
        self._instinct = instinct
        self._renderer = renderer
        self._character_profile = character_profile
        self._memory_root = memory_root
        self._memsearch = memsearch
        self._tools_by_name: dict[str, type] = {
            cls.__name__: cls for cls in TOOLS
        }
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    def dispatch(self, raw: RawEvent) -> None:
        if self._stopping:
            raise RuntimeError("EventDispatcher is stopping")
        task = asyncio.create_task(
            self._handle(raw), name=f"event-{type(raw).__name__}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        self._stopping = True
        if not self._tasks:
            return
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

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
            sink.put_nowait(None)

    async def _perceive(self, raw: RawEvent) -> DollEvent:
        if isinstance(raw, UserTextEvent):
            return DollEvent(perception=raw.text, raw=raw)
        raise TypeError(f"no stub perceive for {type(raw).__name__}")

    async def _respond(
        self,
        doll_event: DollEvent,
        summary: str,
        sink: asyncio.Queue[ServerMessage | None],
    ) -> None:
        recall = await self._inner_voice.recall(doll_event.perception)
        system = self._renderer.render(
            "scaffolding", character=self._character_profile
        )
        state_block = f"STATE:\n{summary}\n\n" if summary else ""
        prefill = f"{state_block}{recall}DECISION: "

        parser = ToolStreamParser()
        ctx = ToolCtx(
            sink=sink,
            memory_root=self._memory_root,
            memsearch=self._memsearch,
        )
        async for chunk in self._adapter.stream_completion(
            system=system,
            user=doll_event.perception,
            prefill=prefill,
            tools=TOOLS,
        ):
            for call in parser.feed(chunk.text):
                await self._dispatch_tool_call(call, ctx)
            if chunk.done:
                break
        for call in parser.flush():
            await self._dispatch_tool_call(call, ctx)
        ctx.sink.put_nowait(TurnEnd())

    async def _dispatch_tool_call(self, call: dict, ctx: ToolCtx) -> None:
        name = call.get("name")
        tool_cls = self._tools_by_name.get(name) if isinstance(name, str) else None
        if tool_cls is None:
            logger.warning("unknown tool: %r", name)
            return
        try:
            tool = tool_cls.model_validate(call.get("arguments", {}))
        except ValidationError as e:
            logger.warning("tool args validation failed for %s: %s", name, e)
            return
        try:
            await tool.run(ctx)
        except Exception as e:
            logger.exception("tool %s raised", name)
            ctx.sink.put_nowait(ErrorMsg(message=f"tool {name} error: {e}"))

    @staticmethod
    def _sink_of(raw: RawEvent) -> asyncio.Queue[ServerMessage | None]:
        if isinstance(raw, UserTextEvent):
            return raw.response_sink
        raise TypeError(f"no sink for {type(raw).__name__}")
```

### Step 3: Run tests

- [ ] Run: `uv run pytest tests/test_dispatcher.py -q`
- [ ] Expected: dispatcher tests pass (existing post-update + 7 new). Kernel/e2e fail until Task 6.

### Step 4: Lint

- [ ] Run: `uv run ruff check src/dollos/dispatcher.py tests/test_dispatcher.py`
- [ ] Expected: clean.

### Step 5: Commit

- [ ] Run:

```bash
git add src/dollos/dispatcher.py tests/test_dispatcher.py
git commit -m "$(cat <<'EOF'
feat(dispatcher): parser-driven tool dispatch in _respond

EventDispatcher feeds big-model stream into ToolStreamParser; each
parsed <tool_call> is validated via pydantic.model_validate and run
via Tool.run(ctx). Naked text dropped. Multi-tool per round, stream-
order serial execute. ctor adds memory_root + memsearch for ToolCtx
construction. Single-round (no cascade); tool errors emit ErrorMsg
without aborting subsequent tools.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Kernel wiring + e2e fix

**Files:**
- Modify: `src/dollos/kernel.py`
- Modify: `tests/test_kernel.py`
- Modify: `tests/test_e2e.py`

`DollOS.__init__` passes `memory_root` and `memsearch` into `EventDispatcher`. Existing kernel/e2e tests update for new EventDispatcher signature and tool-call-shaped output.

### Step 1: Inspect failures

- [ ] Run: `uv run pytest tests/test_kernel.py tests/test_e2e.py -q`
- [ ] Expected: TypeError EventDispatcher missing memory_root/memsearch; e2e prefill assertion may also be broken if test stream uses old text format.

### Step 2: Implement

- [ ] Edit `src/dollos/kernel.py`. Update `DollOS.__init__`:

```python
def __init__(self, settings: Settings) -> None:
    self.settings = settings
    self.adapter = build_adapter(settings)
    self.renderer = PromptRenderer()
    self.memsearch = build_memsearch(settings)
    self.inner_voice = build_inner_voice(settings, self.memsearch, self.renderer)
    self.instinct = build_instinct(settings, self.renderer)
    self._character_profile = settings.character.profile_path.read_text()
    self.dispatcher = EventDispatcher(
        adapter=self.adapter,
        inner_voice=self.inner_voice,
        instinct=self.instinct,
        renderer=self.renderer,
        character_profile=self._character_profile,
        memory_root=settings.data.root / "memory",
        memsearch=self.memsearch,
    )
    self.server = WebSocketServer(
        host=settings.ipc.host,
        port=settings.ipc.port,
        handler=self._handle_text_input,
    )
    self._shutdown = asyncio.Event()
```

(Note: `memory_root` is `data/memory` — `NoteMemory.run` then writes to `<root>/shared/<date>.md` per Tool implementation. This matches the existing `build_memsearch` pattern that paths in `data/memory/shared`.)

- [ ] Edit `tests/test_kernel.py`. Find every `EventDispatcher(...)` construction (or fixture that builds one) and add `memory_root=tmp_path` + `memsearch=_FakeMemSearch()` (define `_FakeMemSearch` locally; or reuse pattern from test_dispatcher).

- [ ] Edit `tests/test_e2e.py`:

  1. The fake LLM adapter chunk content needs to be tool-call-shaped. Find the test asserting big-model output reaches sink as TextChunk(text="..."). Update the mock chunks to be:

```python
chunks=[
    StreamChunk(
        text='<tool_call>{"name":"Say","arguments":{"text":"<old expected text>"}}</tool_call>',
        done=False,
    ),
    StreamChunk(text="", done=True),
]
```

  2. `EventDispatcher` construction (if direct in test) gets `memory_root=tmp_path` + `memsearch=fake_or_real`. If e2e uses real `DollOS(settings)`, the kernel changes above handle it; just set `settings.data.root` to a tmp_path.

### Step 3: Run

- [ ] Run: `uv run pytest -q`
- [ ] Expected: all green.

### Step 4: Lint

- [ ] Run: `uv run ruff check src/dollos/kernel.py tests/test_kernel.py tests/test_e2e.py`
- [ ] Expected: clean.

### Step 5: Commit

- [ ] Run:

```bash
git add src/dollos/kernel.py tests/test_kernel.py tests/test_e2e.py
git commit -m "$(cat <<'EOF'
feat(kernel): pass memory_root + memsearch into EventDispatcher

Kernel wires settings.data.root/memory and the existing memsearch into
the dispatcher so ToolCtx can be built. Tests updated for tool-call-
shaped fake adapter output and new ctor kwargs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Manual smoke test (real models, end-to-end)

**Files:** No code; existing `experiments/ws_client.py`.

Validate end-to-end tool-call flow against real Qwen3.6 + Qwen3.5-0.8B servers.

### Step 1: Start servers

- [ ] Big-model server (per CLAUDE.md). Skip if already running on `:8001`.
- [ ] Small-model server. Skip if already running on `:8003`.
- [ ] Verify: `curl -s -o /dev/null -w "8001=%{http_code} 8003=%{http_code}\n" http://localhost:8001/health http://localhost:8003/health`

### Step 2: Seed memory + start daemon

- [ ] Ensure `data/memory/shared/` exists with at least one fact (or rely on prior seed if running on the same machine):

```markdown
# 2026-05-05

- 主人喜歡喝美式咖啡，不加糖。
```

- [ ] Run from worktree root: `uv run python -m dollos --config config.toml > /tmp/dollos.log 2>&1 &`
- [ ] Tail log briefly to confirm "memsearch indexed" + "ipc server listening".

### Step 3: Drive 3-turn conversation

- [ ] Run sequentially:

```bash
uv run python experiments/ws_client.py "我等等想喝咖啡"
uv run python experiments/ws_client.py "那我先去燒水"
uv run python experiments/ws_client.py "你還記得我剛剛說什麼嗎"
```

### Step 4: Verify behavior

- [ ] **Output cleanliness**: ws_client output must NOT contain any `<think>`, `</think>`, `<tool_call>`, `</tool_call>` markers. Only the text Doll says (from `Say` tool) is visible. If markers leak — model didn't follow tool format → tighten system prompt or check parser.
- [ ] **NoteMemory wrote**: `cat data/memory/shared/$(date +%Y-%m-%d).md` should show a new bullet line if the model chose to call NoteMemory in any turn (it may not always — that's fine; behavior depends on model judgment).
- [ ] **Recall on turn 3**: Doll's response should reference the seeded coffee preference — this validates the prefill RECALL path is unchanged.

### Step 5: Document

- [ ] If output is correct → mark Task 7 done.
- [ ] If model leaks naked text — capture an example and either:
  - Tighten `_format_tools_block` system prompt wording (e.g., add explicit "DO NOT output any text outside <tool_call> blocks after </think>"), retest.
  - Accept as known limitation; record in spec §10.
- [ ] If unknown tool errors — capture example, check parser logs.

### Step 6: Stop daemon

- [ ] `pkill -f "python -m dollos"`

### Step 7: Commit if prompt was tuned

- [ ] If `_format_tools_block` was edited:

```bash
git add src/dollos/llm/templates.py
git commit -m "tune(templates): tighten tool-format system prompt based on smoke test"
```

---

## Task 8: Roadmap + CLAUDE.md sync

**Files:**
- Modify: `docs/roadmap.md`
- Modify: `CLAUDE.md`

### Step 1: Update `docs/roadmap.md`

- [ ] In `## 已完成` table, append:

```markdown
| Roadmap step 6 — Tool calling (Say + NoteMemory, pydantic) | Merged |
```

- [ ] In `### 6. Tool calling` heading, mark Merged + replace verbose body with brief Merged-summary:

```
Step 6 minimal scope: pydantic Tool models (Say, NoteMemory) with run(ctx); ToolStreamParser state machine; Qwen3ThinkingTemplate `# Tools` system-prompt section; LLMAdapter tools= plumbing; EventDispatcher parser-driven _respond; Kernel wires memory_root + memsearch.

Smoke-tested: 3-turn conversation; output via Say tool only (no naked-text leak); NoteMemory writes daily markdown + memsearch.index_file synchronously. recall tool / cascade / permission / streamable / fast deferred.

**Demo**：Doll 透過 tool 講話 + 寫 memory；下個 step 是 step 7 Reflex + cascade。
```

### Step 2: Update `CLAUDE.md`

- [ ] In "已完成" plan table, append:

```markdown
| Roadmap step 6 — Tool calling (Say + NoteMemory, pydantic) | Merged |
```

- [ ] Replace "下一個" paragraph (currently step 6) with step-7 brief:

```
**Roadmap step 7 — Reflex + pre + post**：完整 bracket loop。Instinct.process() 加 reflex_calls 輸出（規則命中 → external whitelist tool）。Instinct.review() 階段（approved_calls, continue_thread）。ToolExecutedEvent cascade（reflex / 大模型 approved 都產 event 進 queue）。MAX_ITERATIONS backstop。Doll 自決停止（review continue_thread = False）。完整 roadmap：`docs/roadmap.md`。
```

### Step 3: Verify

- [ ] `uv run pytest -q` — green.
- [ ] `uv run ruff check` — clean.

### Step 4: Commit

- [ ] Run:

```bash
git add docs/roadmap.md CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: mark roadmap step 6 (Tool calling) merged, point to step 7

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Done definition

- [ ] All 8 tasks committed on branch `tool-calling`.
- [ ] `uv run pytest -q` green.
- [ ] `uv run ruff check` clean.
- [ ] Smoke test (Task 7) shows clean tool-mediated output (no marker leak), NoteMemory writes (when model chose), recall still works.
- [ ] Roadmap + CLAUDE.md updated.
- [ ] Ready for `superpowers:finishing-a-development-branch`.
