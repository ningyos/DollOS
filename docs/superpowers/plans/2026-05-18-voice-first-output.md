# Plan 1: Voice-First Output + Cleanup

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace mind_loop's JSON action-array output with `<think>...</think>` + interleaved free-text/`<tool_call>` format. Naked text streams to TTS as Doll's voice via the existing `TTSObservingSink`. Remove Say tool, Think tool, recent_thoughts (all dead after the format change). Sentence-level streaming, audio serialization, and interrupt are **out of scope** — separate plans.

**Architecture:** Subagent path already uses `build_qwen3_think_tool_grammar` + `run_tool_cascade` with `<tool_call>` markers; mind_loop is the only consumer of `build_mind_actions_grammar`. We add `voice_mode=True` to `ToolStreamParser` (emits typed `SpeakChunk`/`ToolCallReady` events instead of dropping outside text) and adapt mind_loop's `_llm_iterate` to consume events: speak chunks → `TextChunk` to sink → already-wired TTS; tool calls → dispatch. Subagent stays unchanged.

**Tech Stack:** Python 3.13, asyncio, pydantic, llama.cpp GBNF.

**Out of scope for Plan 1 (deferred to later plans):**
- Sentence-level chunking of speak output (Plan 2)
- VoiceSession serialized speak queue (Plan 2 — without it, multiple consecutive text_chunks in one turn would speak concurrently)
- Interrupt / cancel propagation (Plan 3)

Because of the deferred items, Plan 1 ships with the **constraint** that each turn should emit **at most one contiguous speak block**, so audio overlap doesn't manifest. The scaffolding prompt nudges that — multi-segment alternation is grammar-allowed but discouraged for this milestone.

---

## File Structure

**New files:**
- `src/dollos/cascade/__init__.py` — package marker (cascade.py becomes cascade/__init__.py? No — `cascade.py` is a module at top level. We add a **package** `dollos.cascade_events`. Avoid collision: name the new package `dollos.stream_events` (a single module, not a package).)
  - **Decision:** create `src/dollos/stream_events.py` as a single module with the event dataclasses. No new package.
- `src/dollos/stream_events.py` — `SpeakChunk`, `ToolCallReady`, `StreamDone` dataclasses
- `tests/test_stream_events.py` — sanity tests for events
- `tests/test_voice_first_parser.py` — ToolStreamParser voice_mode tests
- `scripts/smoke_voice_first.py` — manual E2E smoke

**Modified:**
- `src/dollos/tool_parser.py` — voice_mode flag; emits typed events from outside text
- `src/dollos/llm/templates.py` — `build_voice_first_grammar(tools)` (extract `_build_tool_call_rule` helper from `build_qwen3_think_tool_grammar` so both grammars share it)
- `src/dollos/mind/mind_loop.py` — `_llm_iterate` rewritten to consume events; drop `_parse_actions`; drop the `prefill="\n\n</think>\n\n"` (grammar emits the closing tag); add `OutputRecord(kind="Speech", ...)` writes for each completed speak segment
- `src/dollos/mind/mind_state.py` — drop `recent_thoughts` deque + `Thought` dataclass + their (de)serialize hooks
- `src/dollos/mind/mind_prompt.py` — drop `[recent thoughts]` block; rename anti-spam check `kind == "Say"` → `kind == "Speech"`; rendering `said:`→`spoke:`
- `src/dollos/tools.py` — delete `class Say`, `class Think`
- `src/dollos/prompts/templates/scaffolding.jinja` — rewrite to drop JSON action array docs, Say/Think mentions, recent-thoughts block; add voice-framing section
- `tests/test_llm_grammar.py` — voice_first grammar tests; drop tests for `build_mind_actions_grammar`
- `tests/test_tools.py` — drop Say/Think tests
- `tests/test_mind_prompt.py` — Speech instead of Say; drop recent_thoughts tests
- `tests/test_mind_state.py` — drop recent_thoughts (de)serialize tests

**Deleted:**
- `src/dollos/llm/templates.py` — `build_mind_actions_grammar` function (last caller switched away in Task 4)
- `Say` / `Think` Pydantic classes
- `Thought` dataclass + `recent_thoughts` field

---

## Task 1: Stream event dataclasses

**Files:**
- Create: `src/dollos/stream_events.py`
- Create: `tests/test_stream_events.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_stream_events.py
def test_stream_events_construct():
    from dollos.stream_events import SpeakChunk, ToolCallReady, StreamDone
    a = SpeakChunk(text="hello")
    b = ToolCallReady(name="NoteMemory", arguments={"text": "x"})
    c = StreamDone()
    assert a.text == "hello"
    assert b.name == "NoteMemory"
    assert b.arguments == {"text": "x"}
    assert isinstance(c, StreamDone)


def test_stream_events_are_frozen():
    """Events are immutable to keep the type clean."""
    import dataclasses
    from dollos.stream_events import SpeakChunk
    a = SpeakChunk(text="x")
    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.text = "y"
```

- [ ] **Step 2: Run failing**

```
uv run pytest tests/test_stream_events.py -v
```

Expected: `ModuleNotFoundError: dollos.stream_events`

- [ ] **Step 3: Implement**

```python
# src/dollos/stream_events.py
"""Typed events emitted by the streaming parser as it consumes LLM tokens."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class SpeakChunk:
    """A chunk of text outside <think>/<tool_call>. Stream to TTS."""
    text: str


@dataclass(frozen=True)
class ToolCallReady:
    """A complete <tool_call>...</tool_call> has been parsed."""
    name: str
    arguments: dict


@dataclass(frozen=True)
class StreamDone:
    """LLM stream finished."""


StreamEvent = Union[SpeakChunk, ToolCallReady, StreamDone]
```

- [ ] **Step 4: Run, expect pass**

```
uv run pytest tests/test_stream_events.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/dollos/stream_events.py tests/test_stream_events.py
git commit -m "feat(stream): event dataclasses for voice_first parser"
```

---

## Task 2: ToolStreamParser voice_mode

**Files:**
- Modify: `src/dollos/tool_parser.py`
- Create: `tests/test_voice_first_parser.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_voice_first_parser.py
import pytest

from dollos.stream_events import SpeakChunk, ToolCallReady
from dollos.tool_parser import ToolStreamParser


def test_voice_mode_emits_speak_chunks():
    p = ToolStreamParser(voice_mode=True)
    events = []
    events.extend(p.feed("Hello "))
    events.extend(p.feed('there<tool_call>\n{"name": "NoteMemory", "arguments": {"text": "ok"}}\n</tool_call>'))
    events.extend(p.feed(" bye"))
    events.extend(p.flush())

    speaks = [e for e in events if isinstance(e, SpeakChunk)]
    tools = [e for e in events if isinstance(e, ToolCallReady)]
    assert "".join(s.text for s in speaks) == "Hello there bye"
    assert len(tools) == 1
    assert tools[0].name == "NoteMemory"
    assert tools[0].arguments == {"text": "ok"}


def test_voice_mode_split_open_marker_no_leak():
    """A tool_call open marker split across feed() boundaries must not leak."""
    p = ToolStreamParser(voice_mode=True)
    events = []
    events.extend(p.feed("text <tool"))
    events.extend(p.feed('_call>\n{"name":"NoteMemory","arguments":{"text":"hi"}}\n</tool_call>'))
    events.extend(p.flush())
    speaks = [e for e in events if isinstance(e, SpeakChunk)]
    # Outside should be exactly "text " — the "<tool" was retained as lookahead, not speak
    assert "".join(s.text for s in speaks) == "text "


def test_voice_mode_split_close_marker_no_leak():
    """Close marker split across feed() boundaries must finish the tool_call."""
    p = ToolStreamParser(voice_mode=True)
    events = []
    events.extend(p.feed('<tool_call>\n{"name":"NoteMemory","arguments":{"text":"hi"}}\n</tool_'))
    events.extend(p.feed("call>after"))
    events.extend(p.flush())
    speaks = [e for e in events if isinstance(e, SpeakChunk)]
    tools = [e for e in events if isinstance(e, ToolCallReady)]
    assert len(tools) == 1
    assert "".join(s.text for s in speaks) == "after"


def test_voice_mode_invalid_json_in_tool_dropped():
    """Malformed tool_call JSON drops the block + emits warning (no crash)."""
    p = ToolStreamParser(voice_mode=True)
    events = []
    events.extend(p.feed("<tool_call>\nnot json\n</tool_call>"))
    events.extend(p.flush())
    assert [e for e in events if isinstance(e, ToolCallReady)] == []


def test_voice_mode_unclosed_tool_at_flush_drops():
    p = ToolStreamParser(voice_mode=True)
    p.feed('<tool_call>\n{"name":"NoteMemory","arguments":{"text":"hi"}}')
    events = p.flush()
    assert [e for e in events if isinstance(e, ToolCallReady)] == []


def test_legacy_default_mode_unchanged():
    """voice_mode=False keeps legacy drop-outside-text policy and dict output."""
    p = ToolStreamParser()
    calls = []
    calls.extend(p.feed("ignored "))
    calls.extend(p.feed('<tool_call>\n{"name":"X","arguments":{}}\n</tool_call>'))
    calls.extend(p.flush())
    assert isinstance(calls[0], dict)
    assert calls[0]["name"] == "X"
```

- [ ] **Step 2: Run, expect TypeError (no `voice_mode` arg)**

```
uv run pytest tests/test_voice_first_parser.py -v
```

- [ ] **Step 3: Implement voice_mode**

Edit `src/dollos/tool_parser.py`:

```python
from dollos.stream_events import SpeakChunk, ToolCallReady


class ToolStreamParser:
    def __init__(self, voice_mode: bool = False) -> None:
        self._state = _State.OUTSIDE
        self._buf = ""
        self._inside_buf = ""
        self._voice_mode = voice_mode

    def feed(self, chunk: str):
        """Legacy mode → list[dict]; voice mode → list[SpeakChunk | ToolCallReady]."""
        self._buf += chunk
        if self._voice_mode:
            return list(self._feed_voice())
        return list(self._feed_legacy())

    def _feed_voice(self):
        while True:
            if self._state is _State.OUTSIDE:
                i = self._buf.find(OPEN)
                if i == -1:
                    # No OPEN found. Hold back len(OPEN)-1 bytes for lookahead.
                    safe = max(0, len(self._buf) - (len(OPEN) - 1))
                    if safe > 0:
                        yield SpeakChunk(text=self._buf[:safe])
                        self._buf = self._buf[safe:]
                    return
                if i > 0:
                    yield SpeakChunk(text=self._buf[:i])
                self._buf = self._buf[i + len(OPEN):]
                self._state = _State.INSIDE
                self._inside_buf = ""
            else:  # INSIDE
                j = self._buf.find(CLOSE)
                if j == -1:
                    # Hold back len(CLOSE)-1 chars for lookahead so split CLOSE doesn't leak as inside.
                    safe = max(0, len(self._buf) - (len(CLOSE) - 1))
                    if safe > 0:
                        self._inside_buf += self._buf[:safe]
                        self._buf = self._buf[safe:]
                    return
                self._inside_buf += self._buf[:j]
                self._buf = self._buf[j + len(CLOSE):]
                payload = self._inside_buf.strip()
                self._inside_buf = ""
                self._state = _State.OUTSIDE
                try:
                    obj = json.loads(payload)
                    if isinstance(obj, dict) and "name" in obj:
                        yield ToolCallReady(name=obj["name"], arguments=obj.get("arguments") or {})
                    else:
                        logger.warning("tool_call payload missing name; dropping")
                except json.JSONDecodeError as e:
                    logger.warning("tool_call JSON decode failed: %s", e)

    def _feed_legacy(self):
        # Lift the existing body of feed() verbatim here.
        # (Move all the existing state-machine code from feed() into this method.)
        # Keep imports identical.
        ...
        # (return generator of dicts)

    def flush(self):
        if self._voice_mode:
            return list(self._flush_voice())
        return list(self._flush_legacy())

    def _flush_voice(self):
        if self._state is _State.OUTSIDE and self._buf:
            yield SpeakChunk(text=self._buf)
            self._buf = ""
        elif self._state is _State.INSIDE:
            logger.warning("voice flush with unclosed <tool_call>; dropping inside buf")
            self._inside_buf = ""
            self._buf = ""
            self._state = _State.OUTSIDE

    def _flush_legacy(self):
        # Move existing flush() body here verbatim.
        ...
```

**Concrete refactor**: take the current `feed()` body, rename to `_feed_legacy()`, return generator. Take current `flush()` body, rename to `_flush_legacy()`. Add `_feed_voice` and `_flush_voice` as above. Adjust the public `feed()`/`flush()` to dispatch on `self._voice_mode`.

- [ ] **Step 4: Run all parser tests, expect pass**

```
uv run pytest tests/test_voice_first_parser.py tests/test_tool_parser.py -v
```

(The existing `test_tool_parser.py` exercises legacy mode — must still pass.)

- [ ] **Step 5: Commit**

```bash
git add src/dollos/tool_parser.py tests/test_voice_first_parser.py
git commit -m "feat(parser): ToolStreamParser voice_mode emits typed events"
```

---

## Task 3: `build_voice_first_grammar` + shared `_build_tool_call_rule`

**Files:**
- Modify: `src/dollos/llm/templates.py`
- Modify: `tests/test_llm_grammar.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_llm_grammar.py — append

def test_voice_first_grammar_smoke():
    from dollos.tools import NoteMemory
    from dollos.llm.templates import build_voice_first_grammar
    g = build_voice_first_grammar([NoteMemory])
    assert "root ::=" in g
    assert "</think>" in g
    assert "<tool_call>" in g
    assert "NoteMemory" in g
    # Segments rule allows zero or more speak/tool-call pieces
    assert "segments" in g


def test_voice_first_grammar_rejects_empty_tools():
    from dollos.llm.templates import build_voice_first_grammar
    import pytest
    with pytest.raises(ValueError):
        build_voice_first_grammar([])


def test_voice_first_grammar_accepts_silent_finish():
    """Grammar must permit think then no segments (silent turn)."""
    from dollos.tools import NoteMemory
    from dollos.llm.templates import build_voice_first_grammar
    g = build_voice_first_grammar([NoteMemory])
    # segments ::= segment*  → matches zero
    assert "segment*" in g or "segments ::=" in g
```

- [ ] **Step 2: Run, expect failure**

```
uv run pytest tests/test_llm_grammar.py::test_voice_first_grammar_smoke -v
```

- [ ] **Step 3: Refactor existing `build_qwen3_think_tool_grammar`**

Extract a helper that returns `(rule_id, rule_text)` for a given tool. The existing function (lines 116–296 in `templates.py`) builds tool-call rules inline; lift that body into `_build_tool_call_rule(tool: type[BaseModel]) -> tuple[str, str]` and call it from both `build_qwen3_think_tool_grammar` and the new `build_voice_first_grammar`.

```python
# src/dollos/llm/templates.py — append

def _build_tool_call_rule(tool: type[BaseModel]) -> tuple[str, str]:
    """Build a single tool-call rule. Returns (rule_id, rule_text).

    Extracted from build_qwen3_think_tool_grammar so multiple grammars share it.
    """
    # [Lift the body of the tool-name loop from build_qwen3_think_tool_grammar:
    #  - Iterate model_fields, build body_parts based on types
    #  - Construct literal: <tool_call>\n{"name": "...", "arguments": {...}}\n</tool_call>
    #  - Return rule_id and the GBNF rule text]
    name = tool.__name__
    body_parts = []
    for fname, field in tool.model_fields.items():
        ftype = field.annotation
        if ftype is str:
            body_parts.append(f'\\"{fname}\\": json-str')
        elif ftype is int:
            body_parts.append(f'\\"{fname}\\": json-int')
        elif ftype is bool:
            body_parts.append(f'\\"{fname}\\": json-bool')
        # ... (mirror existing logic in build_qwen3_think_tool_grammar; raise
        # ValueError for unsupported types)
        else:
            raise ValueError(
                f"tool {name} required field {fname!r} has unsupported type {ftype!r}"
            )
    joined = ", ".join(body_parts) if len(body_parts) > 1 else (body_parts[0] if body_parts else "")
    rule_id = _rule_id(name)
    rule = (
        f'{rule_id} ::= "<tool_call>\\n'
        f'{{\\"name\\": \\"{name}\\", \\"arguments\\": {{'
        f'{joined}}}}}\\n</tool_call>"'
    )
    return rule_id, rule


def build_voice_first_grammar(tools: list[type[BaseModel]]) -> str:
    """Build GBNF for voice-first cascade output.

    Output shape:
        <think>SEEN: ...
        INTENT: ...
        REVIEW: ...
        MOOD: ...
        TOOL: ...
        </think>\\n\\n
        (segment)*
    where segment = speak | tool-call.

    Speak segments are any byte run not overlapping a <tool_call> opening.
    The grammar uses a permissive character class for speak — we accept any
    byte except '<' and let the parser's split-marker lookahead handle the
    boundary at runtime.
    """
    if not tools:
        raise ValueError("tools must be non-empty for voice_first grammar build")

    for tool in tools:
        if "\\" in tool.__name__ or '"' in tool.__name__:
            raise ValueError(
                f"tool name {tool.__name__!r} contains backslash/quote; unsupported"
            )

    rule_ids: list[str] = []
    rules: list[str] = []
    for tool in tools:
        rid, rtext = _build_tool_call_rule(tool)
        rule_ids.append(rid)
        rules.append(rtext)

    tool_call_alts = " | ".join(rule_ids)
    head = (
        "root ::= think segments\n"
        'think ::= "SEEN: " line "INTENT: " line "REVIEW: " line '
        '"MOOD: " line "TOOL: " line "</think>\\n\\n"\n'
        'line ::= [^\\n]+ "\\n"\n'
        # segments may be zero (silent turn). speak is a permissive run of
        # non-'<' bytes; the parser distinguishes between a speak '<' and a
        # tool_call opener at runtime via lookahead.
        "segments ::= segment*\n"
        "segment ::= speak | tool-call\n"
        "speak ::= [^<]+\n"
        f"tool-call ::= {tool_call_alts}\n"
    )
    body = "\n".join(rules) + "\n"
    return head + body + _JSON_STR_RULES
```

**Note on `speak ::= [^<]+`**: this restricts speak to runs of non-`<` bytes. Doll's natural Chinese / English text doesn't use `<` so this is fine. If she ever needs to vocalize a `<`, she'd phrase it differently ("less than"). This trades a 0.1% niche for a much simpler grammar.

- [ ] **Step 4: Run grammar tests, expect pass**

```
uv run pytest tests/test_llm_grammar.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/dollos/llm/templates.py tests/test_llm_grammar.py
git commit -m "feat(grammar): build_voice_first_grammar + shared tool-call rule helper"
```

---

## Task 4: mind_loop switches to voice_first cascade

**Files:**
- Modify: `src/dollos/mind/mind_loop.py`
- Modify: `tests/test_mind_loop.py` (replace JSON parse tests with event-flow tests)

- [ ] **Step 1: Write failing test**

```python
# tests/test_mind_loop.py — replace or extend

@pytest.mark.asyncio
async def test_mind_loop_voice_first_speak_chunks_to_sink():
    """When mind_loop iter runs, naked LLM text reaches the registered sink as TextChunk."""
    from dollos.ipc.messages import TextChunk
    from dollos.mind.mind_state import MindState
    # Boot a minimal mind_loop: mock the adapter to yield:
    # <think>SEEN: ...\n...\n</think>\n\nHello there<tool_call>\n{"name":"NoteMemory","arguments":{"text":"x"}}\n</tool_call> bye
    # Drive _llm_iterate once
    # Drain registered sink and assert TextChunk("Hello there") and TextChunk(" bye") present
    ...
```

(The test fixture is verbose; in practice the subagent should reuse the existing `tests/test_mind_loop.py` boilerplate which constructs a `MindLoop` with a mock LLM. Look at the existing tests for the wiring pattern.)

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Rewrite mind_loop**

In `src/dollos/mind/mind_loop.py`:

```python
# imports — add:
from dollos.ipc.messages import TextChunk
from dollos.stream_events import SpeakChunk, ToolCallReady
from dollos.tool_parser import ToolStreamParser
from dollos.llm.templates import build_voice_first_grammar  # replace build_mind_actions_grammar

# In __init__:
self._grammar = build_voice_first_grammar(list(self._tool_registry.values()))
# (was: build_mind_actions_grammar(list(self._tool_registry.keys())))

# Replace _llm_iterate, _llm_call, _parse_actions with:
async def _llm_iterate(self, prompt: str) -> None:
    """Stream LLM output through voice_first parser. Speak → sink; tool_call → dispatch."""
    sink = self._ctx.sink_resolver()
    parser = ToolStreamParser(voice_mode=True)

    async for chunk in self._llm.stream_completion(
        system="",
        user=prompt,
        prefill="",  # grammar emits the </think> closing tag; no prefill needed
        max_tokens=2048,
        grammar=self._grammar,
        purpose="cascade",
    ):
        if chunk.text:
            for event in parser.feed(chunk.text):
                await self._handle_stream_event(event, sink)
        if chunk.done:
            break

    for event in parser.flush():
        await self._handle_stream_event(event, sink)

async def _handle_stream_event(self, event, sink) -> None:
    if isinstance(event, SpeakChunk):
        if event.text:
            sink.put_nowait(TextChunk(text=event.text))
            self._state.recent_outputs.append(OutputRecord(
                kind="Speech",
                t=time.time(),
                summary=f"spoke: {event.text[:60]}",
            ))
    elif isinstance(event, ToolCallReady):
        await self._dispatch_tool(event.name, event.arguments)

async def _dispatch_tool(self, name: str, arguments: dict) -> None:
    tool_cls = self._tool_registry.get(name)
    if tool_cls is None:
        logger.warning("unknown tool: %s", name)
        return
    try:
        tool = tool_cls(**arguments)
    except ValidationError as e:
        logger.warning("tool validation failed for %s: %s", name, e)
        return
    try:
        await tool.run(self._ctx)
    except Exception:
        logger.exception("tool %s failed", name)
```

Also in `iterate()`: delete the `for action in actions: await action.run(...)` block — dispatching is now inline inside `_llm_iterate`. Just `await self._llm_iterate(prompt)` and proceed to counter / persist.

- [ ] **Step 4: Run mind_loop tests, expect pass**

```
uv run pytest tests/test_mind_loop.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/mind_loop.py tests/test_mind_loop.py
git commit -m "feat(mind_loop): voice_first cascade — streaming text→sink, tool_call→dispatch"
```

---

## Task 5: Rewrite scaffolding prompt

**Files:**
- Modify: `src/dollos/prompts/templates/scaffolding.jinja`
- Modify: `tests/test_prompt_renderer.py`

- [ ] **Step 1: Write failing test**

```python
def test_scaffolding_voice_first():
    out = render_scaffolding(...)
    # New voice framing present
    assert "spoken aloud" in out or "your voice" in out
    # Tool call markup present
    assert "<tool_call>" in out
    # Old JSON action array convention gone
    assert '"action":' not in out
    assert "{\"action\"" not in out
    # Say/Think no longer mentioned
    assert "Say tool" not in out
    assert "Think action" not in out
    # Silent turn convention: empty after think
    assert "nothing" in out.lower() or "silent" in out.lower()
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Rewrite scaffolding.jinja**

The current scaffolding has multiple sections (identity, perception, memory, tools list with JSON action array, body proprioception, cognition, recent thoughts...). Rewrite the **output-format** section. Other sections stay.

Concrete edit — replace the section that documents JSON action arrays (`"action": "Say"`, etc.) with:

```jinja
# Output — your voice

Anything you write outside `<think>` and `<tool_call>` blocks is **streamed
to TTS — the user hears it**. Write conversationally. You're talking, not
writing a memo.

- No markdown, no bullets, no headings — they read badly out loud.
- No code blocks, URLs, or long hashes to vocalize.
- Internal reasoning belongs in `<think>` (not heard).
- Tool invocations belong in `<tool_call>` (not heard).
- Nothing to say? Just close `</think>` and emit nothing.

# Format

```
<think>
SEEN: (what you currently perceive)
INTENT: (what you want to do)
REVIEW: (your progress on it)
MOOD: (a one-line emotional snapshot)
TOOL: (the next action you take — "speak", a tool name, or "none")
</think>

(your spoken text, or empty if silent)

<tool_call>
{"name": "ToolName", "arguments": {...}}
</tool_call>

(more spoken text if you have more to say)
```

You may interleave speak and tool_call segments freely. **For now, prefer
putting a single block of spoken text at the end of your turn** — the
voice pipeline doesn't yet serialize multiple speak segments, so two
back-to-back speak blocks can play over each other. (This restriction
is temporary; see Plan 2.)

# Tools available

{% for tool_name, tool_doc in tools_doc %}
- **{{ tool_name }}** — {{ tool_doc }}
{% endfor %}

(See each tool's pydantic model for required arguments.)
```

Also drop any remaining JSON-array empty-list `[]` convention text. Drop `recent thoughts` block reference (deleted in Task 7).

The `tools_doc` template variable needs to exist — see `src/dollos/prompts/renderer.py` for current variables. Add a `tools_doc` variable built from the tool registry: list of (name, short docstring first line).

- [ ] **Step 4: Add `tools_doc` to renderer**

In `renderer.py`:

```python
def render_scaffolding(..., tool_registry: dict[str, type] | None = None):
    ...
    tools_doc = []
    if tool_registry:
        for name, cls in tool_registry.items():
            doc = (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else ""
            tools_doc.append((name, doc))
    ...
    return template.render(..., tools_doc=tools_doc)
```

Update callers (kernel.py, subagent.py) to pass `tool_registry=...`.

- [ ] **Step 5: Run prompt tests, expect pass**

```
uv run pytest tests/test_prompt_renderer.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/dollos/prompts/templates/scaffolding.jinja src/dollos/prompts/renderer.py tests/test_prompt_renderer.py
git commit -m "feat(prompt): scaffolding rewrite for voice_first output"
```

---

## Task 6: Anti-spam relabel Say→Speech

**Files:**
- Modify: `src/dollos/mind/mind_prompt.py`
- Modify: `tests/test_mind_prompt.py`

(Note: Task 4 already writes `OutputRecord(kind="Speech", ...)` from mind_loop. This task updates the **consumer** in mind_prompt.)

- [ ] **Step 1: Write failing test**

```python
def test_recent_outputs_block_warns_on_recent_speech():
    """Doll spoke <30s ago → WARNING in [Recent outputs] header."""
    from dollos.mind.mind_state import MindState, OutputRecord
    from dollos.mind.mind_prompt import _render_outputs_header
    import time

    state = MindState()
    state.recent_outputs.append(OutputRecord(
        kind="Speech",
        t=time.time() - 10,
        summary="spoke: 主人晚安",
    ))
    out = _render_outputs_header(state.recent_outputs, time.time())
    assert "WARNING" in out
    assert "spoke" in out


def test_recent_outputs_block_no_warning_old_speech():
    """Speech >30s ago → no warning."""
    state = MindState()
    state.recent_outputs.append(OutputRecord(
        kind="Speech",
        t=time.time() - 60,
        summary="spoke: 早安",
    ))
    out = _render_outputs_header(state.recent_outputs, time.time())
    assert "WARNING" not in out
```

- [ ] **Step 2: Run, expect failure** (current code keys on `kind == "Say"`)

- [ ] **Step 3: Update mind_prompt**

In `src/dollos/mind/mind_prompt.py`, find the function checking `last.kind == "Say"` and change to `last.kind == "Speech"`. Adjust the "you just spoke" snippet to use the "spoke:" prefix instead of "said:".

- [ ] **Step 4: Run tests, expect pass**

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/mind_prompt.py tests/test_mind_prompt.py
git commit -m "tune(mind): anti-spam tracks Speech segments"
```

---

## Task 7: Remove Say tool

**Files:**
- Modify: `src/dollos/tools.py`, `tests/test_tools.py`
- Search-and-modify: any character pack / config that references "Say"

- [ ] **Step 1: Inventory references**

```bash
cd ~/Projects/DollOS
grep -rn '"Say"\|class Say\b\|action.*Say' src/ tests/ character_packs/ docs/superpowers/specs/ 2>/dev/null
```

For each reference, decide: drop it (test code, old docs) or update it (active config).

- [ ] **Step 2: Delete `class Say` from tools.py**

Remove the class definition and any `_record(ctx, "Say", ...)` call site (Say.run did this; no callers remain after Task 4).

- [ ] **Step 3: Delete Say tests in tests/test_tools.py**

Drop any `def test_say_*` test.

- [ ] **Step 4: Remove Say from MAIN_TOOLS / SUBAGENT_TOOLS constants if present**

```bash
grep -n "Say," src/dollos/tools.py src/dollos/kernel.py src/dollos/subagent.py
```

Edit out.

- [ ] **Step 5: Run full suite, expect green**

```
uv run pytest
```

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(tools): drop Say — superseded by naked text in voice_first output"
```

---

## Task 8: Remove Think tool + recent_thoughts + Thought class

**Files:**
- Modify: `src/dollos/tools.py`, `src/dollos/mind/mind_state.py`, `src/dollos/mind/mind_prompt.py`, `src/dollos/mind/mind_loop.py`, scaffolding (already handled in Task 5 but verify), tests

- [ ] **Step 1: Delete `class Think` from tools.py**

```bash
grep -n "class Think\b\|\"Think\"\|Think," src/dollos/ -r
```

Verify only the class definition, the registry entry (if any), and tests.

- [ ] **Step 2: Delete `recent_thoughts` deque + `Thought` dataclass from mind_state.py**

In `MindState`:
- Remove `recent_thoughts: deque[Thought] = ...` field
- Remove `Thought` dataclass entirely

In `state_dict_for_persist` / `load_state`: drop the `recent_thoughts` (de)serialize hooks.

- [ ] **Step 3: Delete `[recent thoughts]` block in mind_prompt.py**

Find `_render_thoughts` function + its call site in the main render path. Delete both.

- [ ] **Step 4: Delete JSON-parse fallback path in mind_loop.py**

The old `_parse_actions` had a fallback `treating as Think`. We replaced `_parse_actions` entirely in Task 4 — verify nothing in mind_loop references `Think` cls anymore.

- [ ] **Step 5: Delete Think tests in tests/test_tools.py and recent_thoughts tests in tests/test_mind_state.py and tests/test_mind_prompt.py**

- [ ] **Step 6: Run full suite, expect green**

```
uv run pytest
```

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(mind): drop Think + recent_thoughts + Thought class — empirically unused"
```

---

## Task 9: Drop `build_mind_actions_grammar`

**Files:**
- Modify: `src/dollos/llm/templates.py`, `tests/test_llm_grammar.py`

- [ ] **Step 1: Verify no live caller**

```bash
grep -rn "build_mind_actions_grammar" src/ tests/
```

After Task 4, the only callers should be tests for the function itself.

- [ ] **Step 2: Delete the function**

- [ ] **Step 3: Delete its tests in test_llm_grammar.py**

- [ ] **Step 4: Run full suite**

```
uv run pytest
```

- [ ] **Step 5: Commit**

```bash
git add src/dollos/llm/templates.py tests/test_llm_grammar.py
git commit -m "refactor(grammar): drop build_mind_actions_grammar — superseded"
```

---

## Task 10: E2E smoke

**Files:**
- Create: `scripts/smoke_voice_first.py`

- [ ] **Step 1: Write smoke**

```python
# scripts/smoke_voice_first.py
"""Boot DollOS with voice_first output; send 3 prompts; verify text_chunk + tool dispatch."""
from __future__ import annotations

import asyncio
import json
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

PROMPTS = [
    "你好, Doll",
    "今天台北氣溫是幾度?",
    "幫我記下: 明天要吃早餐",
]


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="dollos_vf_") as tmp:
        s = Settings(
            llm=LLMConfig(
                provider="llamacpp",
                template="qwen3-thinking",
                base_url="http://127.0.0.1:8001",
                model_alias="unsloth/Qwen3.6",
                timeout_s=120.0,
            ),
            ipc=IPCConfig(host="127.0.0.1", port=8767),
            log=LogConfig(level="INFO"),
            data=DataConfig(root=Path(tmp) / "data"),
            memsearch=MemsearchConfig(top_k=5),
            character=CharacterConfig(
                pack=str(REPO_ROOT / "character_packs" / "gura")
            ),
        )
        d = DollOS(s)
        task = asyncio.create_task(d.run())
        await asyncio.sleep(3.0)
        try:
            async with websockets.connect("ws://127.0.0.1:8767") as ws:
                for p in PROMPTS:
                    print(f"\n→ {p}", flush=True)
                    await ws.send(json.dumps({"type": "text_input", "text": p}))
                    end = time.monotonic() + 60.0
                    while time.monotonic() < end:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue
                        m = json.loads(raw)
                        if m.get("type") == "text_chunk":
                            print(f"  speak: {m['text']!r}", flush=True)
                        elif m.get("type") == "turn_end":
                            print("  [turn_end]", flush=True)
                            break
                        else:
                            print(f"  [{m.get('type')}]", flush=True)
                    await asyncio.sleep(1.0)
        finally:
            d._mind_loop.shutdown()
            await asyncio.gather(task, return_exceptions=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: Run** (requires llama-server up on :8001)

```bash
curl -s http://localhost:8001/health || (echo "start llama-server first"; exit 1)
uv run python -u scripts/smoke_voice_first.py 2>&1 | tee /tmp/smoke_voice_first.log
```

Expected:
- Three turns each emit one or more `text_chunk` (Doll speaks naturally)
- The "幫我記下" turn dispatches a NoteMemory `tool_call` silently (no text_chunk for it; visible only if you grep kernel log for "NoteMemory")
- Each turn ends with `turn_end`

- [ ] **Step 3: Manual review**

- Are speak chunks natural sentences (or paragraphs since no sentence splitter yet)?
- Do any turns emit multiple speak segments interleaved with tool calls? If yes, audio would overlap when actually played — note the limitation; revisit when Plan 2 lands.
- Does Doll attempt silent turns (no text_chunk between turn boundaries)?

- [ ] **Step 4: Commit smoke**

```bash
git add scripts/smoke_voice_first.py
git commit -m "test(voice): voice_first e2e smoke"
```

---

## Self-Review Checklist

**Spec coverage:**
- ✅ Output format change (JSON array → think + text + tool_call) → Tasks 1-4
- ✅ Naked text → TTS via existing TTSObservingSink → Task 4 (sink.put_nowait(TextChunk))
- ✅ Doll knows text is voiced → Task 5 (scaffolding)
- ✅ Q1B silent turn = no segments → Task 3 grammar, Task 5 scaffolding ("nothing to say? emit nothing")
- ✅ Q3 multi-segment allowed → Task 3 grammar; Task 5 prompt notes the Plan 1 limitation (audio overlap until Plan 2)
- ✅ Q4 anti-spam relabel → Tasks 4 + 6
- ✅ Q5 voice prompt section → Task 5
- ✅ Think action removed → Task 8
- ✅ Say tool removed → Task 7
- ✅ Subagent unchanged → not touched (subagent.py uses build_qwen3_think_tool_grammar, also not touched)
- ✅ Smoke → Task 10

**Out of scope (in matching plans):**
- Sentence streaming → Plan 2
- VoiceSession serialized speak queue → Plan 2
- Interrupt + cancel + SayAborted → Plan 3

**Type consistency:**
- `voice_mode` param name consistent in parser
- `SpeakChunk`, `ToolCallReady`, `StreamDone` used identically in all tasks
- Sink usage: `sink.put_nowait(TextChunk(text=...))` throughout
- `kind="Speech"` consistent (anti-spam producer in Task 4, consumer in Task 6)

**Placeholder scan:**
- Task 2 Step 3 mentions "Lift the existing body of feed() verbatim here" — concrete instruction for the subagent; not a placeholder
- Task 4 Step 1 says "verbose; reuse boilerplate" — this IS a soft instruction. Acceptable because the existing test file shows the pattern; subagent reads it
- No TBD / TODO / fill-in-later remain

**Known limitation (documented in scaffolding):**
- Plan 1 ships with the request: prefer single trailing speak segment per turn. Multi-segment interleaving works grammatically + parses correctly, but audio playback will overlap because each text_chunk fires its own concurrent speak task. Plan 2 addresses this with serialization.

---

**Plan complete.**
