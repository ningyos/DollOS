# Scratchpad Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 2000-char ephemeral working-memory notepad (Scratchpad) auto-rendered at the top of every Doll perception, with four pydantic tools (Write / Append / Edit / Clear) for Doll to manage it. Solves the T2-forgetting problem observed in the tool-output-paging e2e smoke.

**Architecture:** A single `Scratchpad` class holds in-memory text bounded at 2000 chars. The dispatcher renders a `[Scratchpad]` block at the top of every assembled perception. Four pydantic `BaseModel` tools (`WriteScratchpad` / `AppendScratchpad` / `EditScratchpad` / `ClearScratchpad`) mutate the scratchpad. Each subagent gets a fresh independent Scratchpad; Doll's instance is created at daemon startup and lives until shutdown.

**Tech Stack:** Python 3.13, asyncio, pydantic, pytest. No new third-party deps.

**Spec:** `docs/superpowers/specs/2026-05-16-scratchpad-design.md`.

---

## File Structure

- **Create:** `src/dollos/scratchpad.py` — `Scratchpad` class + four pydantic tool subclasses.
- **Modify:** `src/dollos/tools.py` — `ToolCtx` and `SubagentToolCtx` get required `scratchpad: Scratchpad` field. New tools added to `MAIN_TOOLS` + `SUB_TOOLS` lists.
- **Modify:** `src/dollos/dispatcher.py` — `EventDispatcher.__init__` gets required `scratchpad: Scratchpad` kwarg; perception-block builder prepends `[Scratchpad]\n<contents>\n\n` (or `(empty)`).
- **Modify:** `src/dollos/subagent.py` — `SubagentRunner` creates a fresh `Scratchpad()` per spawned subagent; passes it to `SubagentToolCtx`.
- **Modify:** `src/dollos/kernel.py` — instantiate `Scratchpad` at startup, pass to `EventDispatcher`.
- **Modify:** `src/dollos/prompts/templates/scaffolding.jinja` — new Behavior bullet.
- **Modify:** `src/dollos/prompts/templates/subagent_scaffolding.jinja` — new Behavior bullet (subagent variant).
- **Create:** `tests/test_scratchpad.py` — Scratchpad class unit tests.
- **Modify:** `tests/test_tools.py` — 4 tool tests + updated `_make_ctx` to accept scratchpad.
- **Modify:** `tests/test_dispatcher_perception.py` — perception block assertions.
- **Modify:** `tests/test_kernel.py` — kernel wiring assertion.
- **Modify:** `tests/_dispatcher_helpers.py` + `tests/test_subagent.py` + other `ToolCtx` / `EventDispatcher` / `SubagentRunner` callsites — pass a `Scratchpad()` instance.
- **Create:** `scripts/smoke_doll_scratchpad_e2e.py` — real-LLM smoke (no pytest assertion, observation only).

---

## Task 1: Scratchpad class + unit tests

**Files:**
- Create: `src/dollos/scratchpad.py`
- Test: `tests/test_scratchpad.py`

- [ ] **Step 1: Write failing tests for Scratchpad core behavior**

```python
# tests/test_scratchpad.py
import pytest

from dollos.scratchpad import Scratchpad


def test_initial_state_empty() -> None:
    sp = Scratchpad()
    assert sp.read() == ""


def test_write_then_read() -> None:
    sp = Scratchpad()
    sp.write("hello")
    assert sp.read() == "hello"


def test_write_overwrites() -> None:
    sp = Scratchpad()
    sp.write("first")
    sp.write("second")
    assert sp.read() == "second"


def test_write_exceeds_cap_raises() -> None:
    sp = Scratchpad()
    with pytest.raises(ValueError, match="exceeds 2000 char cap"):
        sp.write("x" * 2001)


def test_write_at_exact_cap_succeeds() -> None:
    sp = Scratchpad()
    sp.write("x" * 2000)
    assert len(sp.read()) == 2000


def test_append_on_empty_does_not_prepend_newline() -> None:
    sp = Scratchpad()
    sp.append("first line")
    assert sp.read() == "first line"


def test_append_on_nonempty_prepends_newline() -> None:
    sp = Scratchpad()
    sp.write("line one")
    sp.append("line two")
    assert sp.read() == "line one\nline two"


def test_append_returns_new_total_length() -> None:
    sp = Scratchpad()
    sp.write("abc")
    new_total = sp.append("de")  # "abc\nde" = 6 chars
    assert new_total == 6


def test_append_exceeds_cap_raises() -> None:
    sp = Scratchpad()
    sp.write("x" * 1995)
    with pytest.raises(ValueError, match="exceed 2000 chars"):
        sp.append("yyyyy")  # 1995 + 1 (newline) + 5 = 2001


def test_edit_unique_match() -> None:
    sp = Scratchpad()
    sp.write("hello world")
    sp.edit("world", "there")
    assert sp.read() == "hello there"


def test_edit_no_match_raises() -> None:
    sp = Scratchpad()
    sp.write("hello world")
    with pytest.raises(ValueError, match="not found"):
        sp.edit("missing", "anything")


def test_edit_ambiguous_match_raises() -> None:
    sp = Scratchpad()
    sp.write("foo bar foo baz")
    with pytest.raises(ValueError, match="appears 2 times"):
        sp.edit("foo", "x")


def test_edit_overflow_raises() -> None:
    sp = Scratchpad()
    sp.write("x" * 1990 + "needle")  # 1996 chars
    with pytest.raises(ValueError, match="push scratchpad to"):
        sp.edit("needle", "z" * 100)  # would become 2090 chars


def test_clear_resets_to_empty() -> None:
    sp = Scratchpad()
    sp.write("something")
    sp.clear()
    assert sp.read() == ""


def test_clear_then_write_round_trip() -> None:
    sp = Scratchpad()
    sp.write("first")
    sp.clear()
    sp.write("second")
    assert sp.read() == "second"
```

- [ ] **Step 2: Run tests, verify failure**

```bash
uv run pytest tests/test_scratchpad.py -v
```
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement Scratchpad class**

```python
# src/dollos/scratchpad.py
"""Scratchpad — Doll's ephemeral working memory.

A 2000-char text document, auto-rendered at the top of every Doll
perception. Doll writes / edits / clears via the four pydantic tools
in this module. Lifetime: daemon process. Storage: in-memory string,
no file backing.

See docs/superpowers/specs/2026-05-16-scratchpad-design.md.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from dollos.tools import ToolCtx


class Scratchpad:
    """In-memory working memory for Doll. Bounded at 2000 chars."""

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

(Pydantic tool classes will be added in Task 4. Keep the file scoped for now.)

- [ ] **Step 4: Run tests, verify pass**

```bash
uv run pytest tests/test_scratchpad.py -v
```
Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dollos/scratchpad.py tests/test_scratchpad.py
git commit -m "feat(scratchpad): add Scratchpad class with write/append/edit/clear"
```

---

## Task 2: Wire Scratchpad to ToolCtx + dispatcher + kernel + subagent

**Files:**
- Modify: `src/dollos/tools.py` (ToolCtx + SubagentToolCtx)
- Modify: `src/dollos/dispatcher.py` (EventDispatcher constructor)
- Modify: `src/dollos/subagent.py` (SubagentRunner constructs per-spawn Scratchpad)
- Modify: `src/dollos/kernel.py` (instantiate Scratchpad at startup)
- Test: `tests/test_kernel.py`

Lessons from the tool-output-paging plan (`docs/superpowers/plans/2026-05-15-tool-output-paging.md`, Task 2): make the new field **required (no `Optional`, no `= None`)**. The "No fallback mechanisms" rule applies. Update all existing `ToolCtx(...)` / `EventDispatcher(...)` / `SubagentRunner(...)` callsites in tests to pass a `Scratchpad()` instance.

- [ ] **Step 1: Add scratchpad field to ToolCtx and SubagentToolCtx**

In `src/dollos/tools.py`, add a TYPE_CHECKING import at the top:

```python
if TYPE_CHECKING:
    ...
    from dollos.scratchpad import Scratchpad
```

In the `ToolCtx` dataclass (after `tool_output_store`, before the default-having fields):

```python
@dataclass
class ToolCtx:
    sink: ...
    memory_root: Path
    memsearch: "MemSearch"
    transcripts_root: Path
    tool_output_store: "ToolOutputStore"
    scratchpad: "Scratchpad"  # NEW — required, no default
    subagent_runner: "SubagentRunner" | None = None
    shell_runner: "ShellRunner" | None = None
    monitor_runner: "MonitorRunner" | None = None
```

`SubagentToolCtx` inherits from ToolCtx — if it overrides fields, ensure scratchpad is present.

- [ ] **Step 2: Add scratchpad kwarg to EventDispatcher**

In `src/dollos/dispatcher.py`, find `class EventDispatcher` and update `__init__`:

```python
from dollos.scratchpad import Scratchpad  # top-level import

class EventDispatcher:
    def __init__(
        self,
        *,
        ...existing args,
        tool_output_store: "ToolOutputStore",
        scratchpad: Scratchpad,  # NEW required
    ) -> None:
        ...
        self._tool_output_store = tool_output_store
        self._scratchpad = scratchpad
```

Wherever `EventDispatcher` constructs a `ToolCtx` (look around `_respond` or where ToolCtx is built), pass `scratchpad=self._scratchpad`.

- [ ] **Step 3: SubagentRunner constructs a fresh Scratchpad per spawn**

In `src/dollos/subagent.py`:

```python
from dollos.scratchpad import Scratchpad  # top-level import
```

Find where `SubagentToolCtx` is constructed inside `_run_cascade` (search for `SubagentToolCtx(`). Add `scratchpad=Scratchpad()` to the args. Each spawned subagent thus gets an independent fresh scratchpad; nothing is shared with Doll's parent scratchpad.

`SubagentRunner.__init__` itself does NOT need a scratchpad arg — only the constructed contexts do.

- [ ] **Step 4: Kernel instantiates scratchpad**

In `src/dollos/kernel.py`:

```python
from dollos.scratchpad import Scratchpad  # top-level import

class DollOS:
    def __init__(self, settings: Settings) -> None:
        ...
        self._scratchpad = Scratchpad()
        ...
        self._dispatcher = EventDispatcher(
            ...,
            tool_output_store=self._tool_output_store,
            scratchpad=self._scratchpad,  # NEW
        )
```

No shutdown cleanup needed — `Scratchpad` is in-memory and vanishes with the process.

- [ ] **Step 5: Update test fixtures**

Find all callsites of `ToolCtx(`, `SubagentToolCtx(`, `EventDispatcher(`, and `SubagentRunner(` in `tests/`:

```bash
grep -rn "ToolCtx(\|SubagentToolCtx(\|EventDispatcher(\|SubagentRunner(" tests/ src/dollos/ --include="*.py"
```

For each test site, add `scratchpad=Scratchpad()` (and import `Scratchpad` at the top of each touched test file). Most live in `tests/_dispatcher_helpers.py`, `tests/test_tools.py`, `tests/test_dispatcher_perception.py`, `tests/test_dispatcher_misc.py`, `tests/test_dispatcher_mood.py`, `tests/test_dispatcher_cascade.py`, `tests/test_dispatcher_pending.py`, `tests/test_dispatcher_wiring.py`, `tests/test_subagent.py`.

- [ ] **Step 6: Write kernel wiring test**

In `tests/test_kernel.py`:

```python
def test_kernel_has_scratchpad() -> None:
    settings = _make_minimal_settings()  # use existing helper
    dollos = DollOS(settings)
    from dollos.scratchpad import Scratchpad
    assert isinstance(dollos._scratchpad, Scratchpad)
    assert dollos._scratchpad.read() == ""
```

If `_make_minimal_settings` doesn't exist in test_kernel.py, copy the pattern from the existing `test_kernel_has_tool_output_store` test.

- [ ] **Step 7: Run full test suite**

```bash
uv run pytest -q
```
Expected: all tests pass. The full suite is large (~420 tests); pre-existing failures (if any from environment, e.g. missing torch) remain unchanged. Any NEW failures are from missed `scratchpad=Scratchpad()` callsites — find and fix.

- [ ] **Step 8: Commit**

```bash
git add src/dollos/tools.py src/dollos/dispatcher.py src/dollos/subagent.py src/dollos/kernel.py tests/
git commit -m "feat(scratchpad): wire Scratchpad into kernel/dispatcher/subagent/ToolCtx"
```

---

## Task 3: Dispatcher renders [Scratchpad] block at top of perception

**Files:**
- Modify: `src/dollos/dispatcher.py` (perception block builder)
- Test: `tests/test_dispatcher_perception.py`

The scratchpad block must appear at the very top of the assembled user message, before `[Memory context]`. Render as `(empty)` placeholder when contents are empty.

- [ ] **Step 1: Find the perception block builder**

```bash
grep -n "_build_perception_blocks\|Memory context\|\[Memory context\]" src/dollos/dispatcher.py | head -5
```

Look for the function that assembles `[Memory context]`, `[Now]`, `[Active monitors]`, `[Pending events]`, etc. into the user message body. Identify where to insert the scratchpad block.

- [ ] **Step 2: Write failing test for empty-scratchpad block**

In `tests/test_dispatcher_perception.py` (add to existing file):

```python
def test_perception_includes_empty_scratchpad_block(_make_dispatcher):
    dispatcher = _make_dispatcher()  # use existing helper; scratchpad defaults to empty
    body = dispatcher._build_perception_blocks(include_static=True, memory_block="[Memory context]\n(no relevant memory)\n\n")
    assert body.startswith("[Scratchpad]\n(empty)\n\n[Memory context]")
```

Adjust to whatever the existing `_build_perception_blocks` signature is — the assertion is the important part: the very first block is `[Scratchpad]\n(empty)\n\n`, followed by Memory context.

- [ ] **Step 3: Write failing test for non-empty scratchpad block**

```python
def test_perception_includes_filled_scratchpad_block(_make_dispatcher):
    dispatcher = _make_dispatcher()
    dispatcher._scratchpad.write("# Current goal\nfind line 150")
    body = dispatcher._build_perception_blocks(include_static=True, memory_block="[Memory context]\n(no relevant memory)\n\n")
    assert body.startswith("[Scratchpad]\n# Current goal\nfind line 150\n\n[Memory context]")
```

- [ ] **Step 4: Run tests, verify failure**

```bash
uv run pytest tests/test_dispatcher_perception.py::test_perception_includes_empty_scratchpad_block tests/test_dispatcher_perception.py::test_perception_includes_filled_scratchpad_block -v
```
Expected: FAIL — no scratchpad block currently rendered.

- [ ] **Step 5: Implement scratchpad block rendering**

In `src/dollos/dispatcher.py`'s perception builder, at the very start of the assembled body, prepend:

```python
contents = self._scratchpad.read()
scratchpad_block = f"[Scratchpad]\n{contents if contents else '(empty)'}\n\n"
body = scratchpad_block + body  # prepended before [Memory context]
```

Or equivalent, depending on the existing builder's shape. The block always appears (even when empty).

- [ ] **Step 6: Run tests, verify pass**

```bash
uv run pytest tests/test_dispatcher_perception.py -v
```
Expected: all PASS, including the 2 new ones.

- [ ] **Step 7: Run full suite — guard against test fixture assertions on body shape**

```bash
uv run pytest -q
```
Expected: any tests that previously asserted exact body prefix or expected blocks in specific order may need updating. Update those assertions to expect the new `[Scratchpad]` block at the top.

- [ ] **Step 8: Commit**

```bash
git add src/dollos/dispatcher.py tests/test_dispatcher_perception.py
git commit -m "feat(scratchpad): render [Scratchpad] block at top of perception"
```

---

## Task 4: WriteScratchpad / AppendScratchpad / EditScratchpad / ClearScratchpad tools

**Files:**
- Modify: `src/dollos/scratchpad.py` (add tool classes)
- Modify: `src/dollos/tools.py` (register in MAIN_TOOLS + SUB_TOOLS)
- Test: `tests/test_tools.py`

- [ ] **Step 1: Write failing tests for all four tools**

In `tests/test_tools.py`:

```python
from dollos.scratchpad import (
    Scratchpad,
    WriteScratchpad,
    AppendScratchpad,
    EditScratchpad,
    ClearScratchpad,
)


@pytest.mark.asyncio
async def test_write_scratchpad_tool(tmp_path):
    sp = Scratchpad()
    ctx = _make_ctx(tmp_path, scratchpad=sp)
    tool = WriteScratchpad(content="hello world")
    result = await tool.run(ctx)
    assert "11 chars" in result
    assert sp.read() == "hello world"


@pytest.mark.asyncio
async def test_write_scratchpad_overflow_raises(tmp_path):
    sp = Scratchpad()
    ctx = _make_ctx(tmp_path, scratchpad=sp)
    tool = WriteScratchpad(content="x" * 2001)
    with pytest.raises(ValueError, match="exceeds 2000"):
        await tool.run(ctx)


@pytest.mark.asyncio
async def test_append_scratchpad_tool(tmp_path):
    sp = Scratchpad()
    sp.write("first")
    ctx = _make_ctx(tmp_path, scratchpad=sp)
    tool = AppendScratchpad(text="second")
    result = await tool.run(ctx)
    assert "12 chars" in result   # "first\nsecond"
    assert sp.read() == "first\nsecond"


@pytest.mark.asyncio
async def test_edit_scratchpad_tool(tmp_path):
    sp = Scratchpad()
    sp.write("hello world")
    ctx = _make_ctx(tmp_path, scratchpad=sp)
    tool = EditScratchpad(old_string="world", new_string="there")
    result = await tool.run(ctx)
    assert "edited" in result
    assert sp.read() == "hello there"


@pytest.mark.asyncio
async def test_edit_scratchpad_no_match_raises(tmp_path):
    sp = Scratchpad()
    sp.write("hello")
    ctx = _make_ctx(tmp_path, scratchpad=sp)
    tool = EditScratchpad(old_string="missing", new_string="x")
    with pytest.raises(ValueError, match="not found"):
        await tool.run(ctx)


@pytest.mark.asyncio
async def test_clear_scratchpad_tool(tmp_path):
    sp = Scratchpad()
    sp.write("something")
    ctx = _make_ctx(tmp_path, scratchpad=sp)
    tool = ClearScratchpad()
    result = await tool.run(ctx)
    assert "cleared" in result
    assert sp.read() == ""
```

The `_make_ctx` helper from test_tools.py needs to accept `scratchpad` kwarg. Update its signature (probably already done in Task 2 step 5).

- [ ] **Step 2: Run tests, verify failure**

```bash
uv run pytest tests/test_tools.py -k "scratchpad" -v
```
Expected: ImportError — tool classes don't exist yet.

- [ ] **Step 3: Implement tool classes in scratchpad.py**

Add to `src/dollos/scratchpad.py` (below the `Scratchpad` class):

```python
class WriteScratchpad(BaseModel):
    """Overwrite the scratchpad with new content.

    Hard cap 2000 chars. Use this when starting fresh or when existing
    content is irrelevant to current work.
    """

    content: str = Field(..., description="full new scratchpad contents (≤2000 chars)")

    async def run(self, ctx: "ToolCtx") -> str:
        ctx.scratchpad.write(self.content)
        return f"scratchpad set ({len(self.content)} chars)"


class AppendScratchpad(BaseModel):
    """Append a line to the end of the scratchpad.

    A newline separator is auto-prepended if the scratchpad is non-empty.
    Raises ValueError if appending would exceed 2000 chars.
    """

    text: str = Field(..., description="text to append as a new line")

    async def run(self, ctx: "ToolCtx") -> str:
        new_total = ctx.scratchpad.append(self.text)
        return f"scratchpad now {new_total} chars"


class EditScratchpad(BaseModel):
    """Replace a unique substring in the scratchpad.

    Same semantics as Claude Code's Edit tool: old_string must appear
    exactly once in the current contents. Use longer old_string with
    surrounding context if a short substring is ambiguous.
    """

    old_string: str = Field(..., description="exact substring to replace; must appear exactly once")
    new_string: str = Field(..., description="replacement text")

    async def run(self, ctx: "ToolCtx") -> str:
        ctx.scratchpad.edit(self.old_string, self.new_string)
        return "scratchpad edited"


class ClearScratchpad(BaseModel):
    """Wipe the scratchpad to empty."""

    async def run(self, ctx: "ToolCtx") -> str:
        ctx.scratchpad.clear()
        return "scratchpad cleared"
```

- [ ] **Step 4: Register tools in MAIN_TOOLS + SUB_TOOLS**

In `src/dollos/tools.py`, find the `MAIN_TOOLS` and `SUB_TOOLS` list constants near the bottom. Add the four new classes (import them at top of tools.py):

```python
from dollos.scratchpad import (
    WriteScratchpad,
    AppendScratchpad,
    EditScratchpad,
    ClearScratchpad,
)

MAIN_TOOLS = [
    ...existing,
    WriteScratchpad,
    AppendScratchpad,
    EditScratchpad,
    ClearScratchpad,
]

SUB_TOOLS = [
    ...existing,
    WriteScratchpad,
    AppendScratchpad,
    EditScratchpad,
    ClearScratchpad,
]
```

- [ ] **Step 5: Update grammar test if it asserts on the tool list**

```bash
grep -n "expected_rule_ids\|ReadToolOutput\|GrepToolOutput" tests/test_llm_grammar.py
```

If the test has a hardcoded list of expected tools (it does, per the tool-output-paging Task 7 fix), add the four new tools to `expected_rule_ids`:

```python
"WriteScratchpad": "write-scratchpad-call",
"AppendScratchpad": "append-scratchpad-call",
"EditScratchpad": "edit-scratchpad-call",
"ClearScratchpad": "clear-scratchpad-call",
```

- [ ] **Step 6: Run tool tests + grammar test**

```bash
uv run pytest tests/test_tools.py -k "scratchpad" tests/test_llm_grammar.py -v
```
Expected: all PASS.

- [ ] **Step 7: Run full suite**

```bash
uv run pytest -q
```
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/dollos/scratchpad.py src/dollos/tools.py tests/test_tools.py tests/test_llm_grammar.py
git commit -m "feat(scratchpad): add Write/Append/Edit/Clear pydantic tools"
```

---

## Task 5: Scaffolding documentation

**Files:**
- Modify: `src/dollos/prompts/templates/scaffolding.jinja`
- Modify: `src/dollos/prompts/templates/subagent_scaffolding.jinja`
- Test: `tests/test_prompt_renderer.py`

- [ ] **Step 1: Write failing test for scaffolding mentioning scratchpad**

In `tests/test_prompt_renderer.py`:

```python
def test_scaffolding_documents_scratchpad(_renderer, _identity):
    out = _renderer.render("scaffolding", identity=_identity, available_skills=[])
    assert "WriteScratchpad" in out
    assert "AppendScratchpad" in out
    assert "EditScratchpad" in out
    assert "ClearScratchpad" in out
    assert "working memory" in out.lower()


def test_subagent_scaffolding_documents_scratchpad(_renderer):
    out = _renderer.render("subagent_scaffolding")
    assert "WriteScratchpad" in out
    assert "your own private" in out.lower()   # subagent scratchpad is independent
```

Reuse `_renderer` and `_identity` fixtures from the existing test file.

- [ ] **Step 2: Run tests, verify failure**

```bash
uv run pytest tests/test_prompt_renderer.py -k "scratchpad" -v
```
Expected: AssertionError — text not in scaffolding.

- [ ] **Step 3: Update scaffolding.jinja**

In `src/dollos/prompts/templates/scaffolding.jinja`, after the bullet about ReadToolOutput/GrepToolOutput (Behavior section), add:

```jinja
- Scratchpad is your working memory — a 2000-char notepad that persists across turns within this session. Use `WriteScratchpad(content="...")` to set the full contents, `AppendScratchpad(text="...")` to add a line, `EditScratchpad(old_string, new_string)` to replace a unique substring, `ClearScratchpad()` to wipe. **Before firing a Shell or Subagent, write your current goal so you remember it when the result comes back as a new turn.** Clear the scratchpad when the task is done — stale notes confuse you. Suggested structure (markdown convention, not enforced): `# Current goal\n...\n# TODO\n- [ ] step 1\n- [x] step 2 (done)`.
```

- [ ] **Step 4: Update subagent_scaffolding.jinja**

In `src/dollos/prompts/templates/subagent_scaffolding.jinja`, after the ReadToolOutput bullet, add:

```jinja
- Scratchpad is your own private working memory — a 2000-char notepad scoped to this subagent run. Doll's parent scratchpad isn't visible here; yours starts empty and disappears when your task ends. Use `WriteScratchpad / AppendScratchpad / EditScratchpad / ClearScratchpad` to track multi-step state across tool results within your task.
```

- [ ] **Step 5: Run tests, verify pass**

```bash
uv run pytest tests/test_prompt_renderer.py -v
```
Expected: 27+ passed (existing + 2 new).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/prompts/templates/ tests/test_prompt_renderer.py
git commit -m "docs(scaffolding): document Scratchpad tools and convention"
```

---

## Task 6: End-to-end smoke

**Files:**
- Create: `scripts/smoke_doll_scratchpad_e2e.py`

Follow-up to `scripts/smoke_doll_paging_e2e.py`. Same prompt — "run `seq 1 200` and tell me line 150". With scratchpad in place, Doll should now write her goal before Shell, see the scratchpad in T2, recall the goal, and call ReadToolOutput to fetch line 150.

- [ ] **Step 1: Write smoke script**

```python
# scripts/smoke_doll_scratchpad_e2e.py
"""Real-LLM e2e smoke verifying scratchpad solves the T2-forgetting problem.

Requires running llama-servers:
- Big LLM (Doll) at http://127.0.0.1:8001
- Inner Voice small LLM at http://127.0.0.1:8003

Sends Doll a multi-turn task: run `seq 1 200`, find line 150, report.
Observes whether Doll writes the scratchpad before Shell, sees it in T2,
and uses ReadToolOutput to fetch the right line.

Pass criterion (observational): Doll calls ReadToolOutput with offset ≈
149 AND her final Say explicitly identifies "150" as the value of line
150. Not a pytest assertion — log the cascade trace and judge by eye.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import date
from pathlib import Path

import websockets

from dollos.config import (
    CharacterConfig, DataConfig, InnerVoiceConfig, IPCConfig,
    LLMConfig, LogConfig, MemsearchConfig, Settings,
)
from dollos.kernel import DollOS


PROMPT = (
    "I need you to do this for me: run `seq 1 200` in a shell. "
    "After the result comes back, look at line 150 specifically and tell me what it is."
)
LOG_PATH = Path("/tmp/iv_doll_scratchpad_e2e.log")


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="smoke-scratchpad-"))
    pack_dir = Path("character_packs/gura")  # use gura, simpler than powdur for this test

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
    )
    dollos = DollOS(settings)
    dollos._bootstrapped_dates.add(date.today())  # skip DailyPlanEvent

    await dollos.memsearch.index()
    await dollos.server.start()

    received: list[dict] = []
    port = dollos.server.port
    uri = f"ws://127.0.0.1:{port}"

    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "text_input", "text": PROMPT}))

        # Collect all messages until cascade fully ends (multiple turn_ends possible).
        end_count = 0
        try:
            while end_count < 3:  # cap turns
                raw = await asyncio.wait_for(ws.recv(), timeout=120.0)
                msg = json.loads(raw)
                received.append(msg)
                if msg["type"] == "turn_end":
                    end_count += 1
        except asyncio.TimeoutError:
            print(f"timeout after {end_count} turns")

    # Dump trace
    LOG_PATH.write_text(json.dumps(received, indent=2, ensure_ascii=False))
    print(f"trace saved: {LOG_PATH}")

    # Print a summary
    tool_calls = [m for m in received if m.get("type") == "tool_call"]
    print(f"\n=== {len(tool_calls)} tool calls ===")
    for tc in tool_calls:
        print(f"  {tc.get('name')}: {json.dumps(tc.get('arguments', {}), ensure_ascii=False)[:120]}")

    says = [m for m in received if m.get("type") == "say"]
    print(f"\n=== {len(says)} say messages ===")
    for s in says:
        print(f"  {s.get('text', '')[:200]}")

    await dollos.server.stop()
    print("\nDONE — eyeball the log to judge whether scratchpad solved T2 forgetting.")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Verify llama-servers are running**

```bash
curl -s -m 2 http://127.0.0.1:8001/health
curl -s -m 2 http://127.0.0.1:8003/health
```
Both should return `{"status":"ok"}`. If not, start them per CLAUDE.md instructions.

- [ ] **Step 3: Run smoke**

```bash
uv run python scripts/smoke_doll_scratchpad_e2e.py
```
Expected: prints tool call trace + say messages. Trace saved to `/tmp/iv_doll_scratchpad_e2e.log`. Eyeball:
- Did iter 0 of T1 call `WriteScratchpad`?
- In T2 (ShellResultEvent), did Doll call `ReadToolOutput` with offset ≈ 149?
- Did the final Say identify "150" as the line value?

Mark observations as DONE_WITH_CONCERNS if Doll skipped the scratchpad or got the wrong answer — the architecture is in place even if her behavioral adoption needs further prompt tuning.

- [ ] **Step 4: Commit**

```bash
git add scripts/smoke_doll_scratchpad_e2e.py
git commit -m "feat(scratchpad): real-LLM e2e smoke for T2-forgetting fix"
```

---

## Final verification

- [ ] **Step 1: Full suite green**

```bash
uv run pytest -q
```

- [ ] **Step 2: Smoke runs and behavior is verified by eye**

```bash
uv run python scripts/smoke_doll_scratchpad_e2e.py
```

- [ ] **Step 3: Use superpowers:finishing-a-development-branch**
