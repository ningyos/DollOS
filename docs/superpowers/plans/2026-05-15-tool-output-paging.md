# Tool Output Paging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tool results (Shell, Subagent) become virtual paginated documents — full output saved to an ephemeral tempdir, perception shows a preview head + an output ID, and new `ReadToolOutput` / `GrepToolOutput` tools let Doll page through the full body on demand.

**Architecture:** Single `ToolOutputStore` (file-backed, lifecycle-bound tempdir). Runners write full output into the store and pass the assigned ID back via the existing `*ResultEvent`. Dispatcher's perception rendering shows preview head + line count + ID instead of the full body. Two new pydantic tools (`ReadToolOutput`, `GrepToolOutput`) read from the store and return content directly to the cascade.

**Tech Stack:** Python 3.13, asyncio, pydantic, pytest. No new third-party deps.

**Scope:** Shell and Subagent results only. Monitor fires per matched line — lines are short, no need to page. Defer.

**Out of scope:** Cross-session persistence of tool outputs (ephemeral by design — tempdir wipes on daemon shutdown).

---

## File Structure

- **Create:** `src/dollos/tool_outputs.py` — `ToolOutputStore` class: `write() -> id`, `read(id, offset, limit) -> ToolOutputSlice`, `grep(id, pattern, max_matches) -> list[ToolOutputMatch]`, `line_count(id) -> int`, `cleanup()`.
- **Create:** `tests/test_tool_outputs.py` — unit tests for store.
- **Modify:** `src/dollos/events.py` — `ShellResultEvent` and `SubagentResultEvent` get a new `output_id: str | None` field.
- **Modify:** `src/dollos/shell_runner.py` — write full output to store on completion; embed assigned ID in the event; keep `output` field as a preview head only (~10 lines).
- **Modify:** `src/dollos/subagent.py` — same pattern: write full `details` to store; `details` field in event becomes preview head.
- **Modify:** `src/dollos/dispatcher.py` — perception rendering for ShellResult / SubagentResult shows preview + line count + ID, instructs Doll to call `ReadToolOutput` for more.
- **Modify:** `src/dollos/kernel.py` — instantiate `ToolOutputStore` at startup, pass to runners via `ToolCtx`, call `cleanup()` on shutdown.
- **Modify:** `src/dollos/tools.py` — register `ReadToolOutput`, `GrepToolOutput` pydantic models with `run()`.
- **Modify:** `tests/test_dispatcher_misc.py`, `tests/test_shell_runner.py`, `tests/test_subagent.py` — update assertions that check the old "full output" perception/event shape.

---

## Task 1: ToolOutputStore

**Files:**
- Create: `src/dollos/tool_outputs.py`
- Test: `tests/test_tool_outputs.py`

- [ ] **Step 1: Write failing test for write+read roundtrip**

```python
# tests/test_tool_outputs.py
from pathlib import Path

import pytest

from dollos.tool_outputs import ToolOutputStore


def test_write_then_read_full(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("hello\nworld\nthis is line three\n")
    assert output_id.startswith("out-")
    slice_ = store.read(output_id, offset=0, limit=10)
    assert slice_.lines == ["hello", "world", "this is line three"]
    assert slice_.total_lines == 3
    assert slice_.start_offset == 0
    assert slice_.end_offset == 3
```

- [ ] **Step 2: Run test, verify failure**

```bash
uv run pytest tests/test_tool_outputs.py -v
```
Expected: ImportError / module not found.

- [ ] **Step 3: Implement ToolOutputStore (minimal)**

```python
# src/dollos/tool_outputs.py
"""Ephemeral file-backed store for tool outputs (Shell stdout, Subagent details).

Lifecycle: created at daemon startup with a tempdir, every tool runner
calls `write()` with the full output and gets back an ID. Doll's
`ReadToolOutput` / `GrepToolOutput` tools call `read()` / `grep()`
against the same store. `cleanup()` runs at daemon shutdown.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class ToolOutputSlice:
    """A slice of a stored output: zero-indexed line range, no trailing newlines."""

    output_id: str
    lines: list[str]
    start_offset: int
    end_offset: int
    total_lines: int


@dataclass
class ToolOutputMatch:
    """A grep match: line index + the matched line text."""

    line_index: int
    line: str


class ToolOutputStore:
    """File-backed store keyed by short opaque ID.

    Each tool output is written to `<root>/<id>.txt` verbatim (preserves
    trailing whitespace; line endings normalized to LF on read).
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def write(self, content: str) -> str:
        output_id = f"out-{uuid.uuid4().hex[:8]}"
        path = self._root / f"{output_id}.txt"
        path.write_text(content, encoding="utf-8")
        return output_id

    def line_count(self, output_id: str) -> int:
        path = self._path(output_id)
        text = path.read_text(encoding="utf-8")
        return _split_lines(text).__len__()

    def read(self, output_id: str, *, offset: int, limit: int) -> ToolOutputSlice:
        path = self._path(output_id)
        text = path.read_text(encoding="utf-8")
        all_lines = _split_lines(text)
        if offset < 0:
            offset = max(0, len(all_lines) + offset)
        end = min(len(all_lines), offset + max(0, limit))
        return ToolOutputSlice(
            output_id=output_id,
            lines=all_lines[offset:end],
            start_offset=offset,
            end_offset=end,
            total_lines=len(all_lines),
        )

    def grep(
        self,
        output_id: str,
        *,
        pattern: str,
        max_matches: int = 20,
    ) -> list[ToolOutputMatch]:
        regex = re.compile(pattern)
        path = self._path(output_id)
        text = path.read_text(encoding="utf-8")
        all_lines = _split_lines(text)
        out: list[ToolOutputMatch] = []
        for i, line in enumerate(all_lines):
            if regex.search(line):
                out.append(ToolOutputMatch(line_index=i, line=line))
                if len(out) >= max_matches:
                    break
        return out

    def cleanup(self) -> None:
        """Delete the entire root dir. Idempotent."""
        import shutil

        shutil.rmtree(self._root, ignore_errors=True)

    def _path(self, output_id: str) -> Path:
        # Strict allowlist: prevent path traversal via id.
        if not output_id.startswith("out-") or not output_id[4:].isalnum():
            raise ValueError(f"invalid tool output id: {output_id!r}")
        p = self._root / f"{output_id}.txt"
        if not p.exists():
            raise FileNotFoundError(f"tool output not found: {output_id}")
        return p


def _split_lines(text: str) -> list[str]:
    # splitlines() drops a final trailing newline; we want one line per logical line.
    return text.splitlines()
```

- [ ] **Step 4: Run test, verify pass**

```bash
uv run pytest tests/test_tool_outputs.py -v
```
Expected: PASS.

- [ ] **Step 5: Add tests for line_count, paging, grep, invalid id**

```python
def test_line_count(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("a\nb\nc\nd\n")
    assert store.line_count(output_id) == 4


def test_paging(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("\n".join(f"line {i}" for i in range(100)))
    s = store.read(output_id, offset=10, limit=5)
    assert s.lines == ["line 10", "line 11", "line 12", "line 13", "line 14"]
    assert s.start_offset == 10
    assert s.end_offset == 15
    assert s.total_lines == 100


def test_negative_offset_seeks_from_end(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("\n".join(f"line {i}" for i in range(20)))
    s = store.read(output_id, offset=-3, limit=10)
    assert s.lines == ["line 17", "line 18", "line 19"]


def test_grep_matches(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("error: foo\nok\nerror: bar\nok\nerror: baz\n")
    matches = store.grep(output_id, pattern=r"^error:", max_matches=2)
    assert [m.line_index for m in matches] == [0, 2]
    assert matches[0].line == "error: foo"


def test_invalid_id_raises(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    with pytest.raises(ValueError):
        store.read("../etc/passwd", offset=0, limit=10)
    with pytest.raises(FileNotFoundError):
        store.read("out-deadbeef", offset=0, limit=10)


def test_cleanup_idempotent(tmp_path: Path) -> None:
    store_root = tmp_path / "ephemeral"
    store = ToolOutputStore(store_root)
    store.write("x")
    assert store_root.exists()
    store.cleanup()
    assert not store_root.exists()
    store.cleanup()  # second call is a no-op
```

- [ ] **Step 6: Run all store tests**

```bash
uv run pytest tests/test_tool_outputs.py -v
```
Expected: 6 PASS.

- [ ] **Step 7: Commit**

```bash
git add src/dollos/tool_outputs.py tests/test_tool_outputs.py
git commit -m "feat(tool-output): add ToolOutputStore — file-backed page store"
```

---

## Task 2: Wire ToolOutputStore to daemon lifecycle

**Files:**
- Modify: `src/dollos/kernel.py`
- Modify: `src/dollos/tools.py:74-91` (ToolCtx dataclass)
- Test: `tests/test_kernel.py`

- [ ] **Step 1: Read existing ToolCtx**

```bash
grep -A20 "^class ToolCtx" src/dollos/tools.py
```

Expected: dataclass with `sink`, `memory_root`, `memsearch`, `transcripts_root`, `subagent_runner`, `shell_runner`, `monitor_runner` fields.

- [ ] **Step 2: Add tool_output_store field to ToolCtx**

In `src/dollos/tools.py`, add the field:

```python
@dataclass
class ToolCtx:
    sink: ...
    memory_root: Path
    memsearch: "MemSearch"
    transcripts_root: Path
    subagent_runner: "SubagentRunner"
    shell_runner: "ShellRunner"
    monitor_runner: "MonitorRunner"
    tool_output_store: "ToolOutputStore"  # NEW
```

Add the import at top of file:

```python
from dollos.tool_outputs import ToolOutputStore
```

- [ ] **Step 3: Wire store in kernel.py**

In `DollOS.__init__` (around where other infra is wired), instantiate the store using a `tempfile.mkdtemp()` and stash it on `self`:

```python
import tempfile

self._tool_output_dir = Path(tempfile.mkdtemp(prefix="dollos-tools-"))
self._tool_output_store = ToolOutputStore(self._tool_output_dir)
```

Add the import at top of kernel.py:

```python
from dollos.tool_outputs import ToolOutputStore
```

Wherever `ToolCtx` is constructed in the dispatcher / cascade wiring, pass `tool_output_store=self._tool_output_store`.

- [ ] **Step 4: Hook cleanup on shutdown**

In `DollOS.run()` (or wherever shutdown lifecycle lives — find via `grep -n "shutdown\|stop\|aclose" src/dollos/kernel.py`), add to the shutdown path:

```python
self._tool_output_store.cleanup()
```

- [ ] **Step 5: Write test that store is wired**

Find the existing kernel construction test in `tests/test_kernel.py`. Add an assertion that the store is reachable:

```python
def test_kernel_has_tool_output_store() -> None:
    settings = _make_minimal_settings()
    dollos = DollOS(settings)
    assert dollos._tool_output_store is not None
    assert dollos._tool_output_dir.exists()
    dollos._tool_output_store.cleanup()
    assert not dollos._tool_output_dir.exists()
```

If `_make_minimal_settings` doesn't exist, copy the Settings construction pattern from `tests/test_e2e.py` lines 53-69.

- [ ] **Step 6: Run kernel tests**

```bash
uv run pytest tests/test_kernel.py -v
```
Expected: PASS. Full suite next.

- [ ] **Step 7: Run full suite (catches ToolCtx breakage in dispatcher/test_e2e)**

```bash
uv run pytest -q
```
Expected: any pre-existing tests that construct `ToolCtx(...)` directly will fail with missing `tool_output_store` argument. Fix those test fixtures by adding `tool_output_store=ToolOutputStore(tmp_path)`.

- [ ] **Step 8: Commit**

```bash
git add src/dollos/kernel.py src/dollos/tools.py tests/test_kernel.py tests/<other fixed test files>
git commit -m "feat(tool-output): wire ToolOutputStore into kernel lifecycle"
```

---

## Task 3: Shell result paging

**Files:**
- Modify: `src/dollos/events.py` (ShellResultEvent)
- Modify: `src/dollos/shell_runner.py`
- Modify: `src/dollos/dispatcher.py` (Shell perception rendering)
- Test: `tests/test_shell_runner.py`
- Test: `tests/test_dispatcher_misc.py`

- [ ] **Step 1: Add output_id field to ShellResultEvent**

In `src/dollos/events.py`, modify `ShellResultEvent`:

```python
@dataclass
class ShellResultEvent(RawEvent):
    command: str
    status: Literal["ok", "nonzero", "timeout", "error"]
    exit_code: int | None
    output: str   # now preview head only (~10 lines)
    output_id: str | None   # NEW; None only if runner-level error before any output captured
    line_count: int   # NEW; total lines of the full output
    response_sink: asyncio.Queue[ServerMessage | None]
```

- [ ] **Step 2: Write failing test in tests/test_shell_runner.py for new event shape**

```python
@pytest.mark.asyncio
async def test_shell_writes_full_output_to_store_and_returns_id(
    tmp_path: Path,
) -> None:
    store = ToolOutputStore(tmp_path)
    events: list[ShellResultEvent] = []

    def dispatch(e: RawEvent) -> None:
        events.append(e)

    runner = ShellRunner(dispatch_fn=dispatch, tool_output_store=store)
    sink: asyncio.Queue = asyncio.Queue()
    # Generate 100 lines of output
    cmd = "for i in $(seq 1 100); do echo line $i; done"
    await runner.spawn(command=cmd, timeout_s=10.0, response_sink=sink)
    # Wait for event
    for _ in range(50):
        if events:
            break
        await asyncio.sleep(0.1)

    assert len(events) == 1
    evt = events[0]
    assert evt.status == "ok"
    assert evt.line_count == 100
    assert evt.output_id is not None
    # `output` is preview head, not the full thing
    assert len(evt.output.splitlines()) <= 15
    # Store has the full body
    full = store.read(evt.output_id, offset=0, limit=200)
    assert full.total_lines == 100
    assert full.lines[0] == "line 1"
    assert full.lines[99] == "line 100"
```

- [ ] **Step 3: Run test, verify failure**

```bash
uv run pytest tests/test_shell_runner.py::test_shell_writes_full_output_to_store_and_returns_id -v
```
Expected: TypeError (ShellRunner doesn't take `tool_output_store` yet) or AttributeError on `output_id`.

- [ ] **Step 4: Implement in ShellRunner**

In `src/dollos/shell_runner.py`:

```python
SHELL_PREVIEW_LINES = 10


class ShellRunner:
    def __init__(
        self,
        *,
        dispatch_fn: Callable[[RawEvent], None],
        tool_output_store: "ToolOutputStore",
    ) -> None:
        self._dispatch_fn = dispatch_fn
        self._tool_output_store = tool_output_store
```

In `_run` (or wherever the event is fired), replace the old `_truncate` call:

```python
# build full output (stdout + stderr)
full_output = stdout_text + (("\n" + stderr_text) if stderr_text else "")
output_id = self._tool_output_store.write(full_output)
all_lines = full_output.splitlines()
line_count = len(all_lines)
preview_lines = all_lines[:SHELL_PREVIEW_LINES]
preview = "\n".join(preview_lines)

event = ShellResultEvent(
    command=command,
    status=status,
    exit_code=exit_code,
    output=preview,
    output_id=output_id,
    line_count=line_count,
    response_sink=response_sink,
)
self._dispatch_fn(event)
```

Drop the `SHELL_OUTPUT_MAX_CHARS` truncation and the `_truncate` helper — store + preview replaces them.

- [ ] **Step 5: Run shell test, verify pass**

```bash
uv run pytest tests/test_shell_runner.py::test_shell_writes_full_output_to_store_and_returns_id -v
```
Expected: PASS.

- [ ] **Step 6: Update existing shell_runner tests for new signature**

Look at `tests/test_shell_runner.py` for tests that construct `ShellRunner(...)` or assert on `evt.output`. Update:
- Pass `tool_output_store=ToolOutputStore(tmp_path)` in constructor
- Change assertions that expected full output to assert on preview + read from store

- [ ] **Step 7: Update Shell perception rendering in dispatcher.py**

Find the `ShellResultEvent` branch in `src/dollos/dispatcher.py` (around line 363) and replace the perception text. **Generate English perception** (the scaffolding is now English; perception should match) — but keep verb tense consistent with neighbouring perceptions in the same file. If neighbours are still Chinese, use Chinese:

```python
if isinstance(raw, ShellResultEvent):
    body = (
        f"shell command finished:\n"
        f"- command: {raw.command}\n"
        f"- status: {raw.status} (exit {raw.exit_code})\n"
        f"- output_id: {raw.output_id}\n"
        f"- total lines: {raw.line_count}\n"
        f"- preview (first {len(raw.output.splitlines())} lines):\n{raw.output}\n"
        f"(use ReadToolOutput(id='{raw.output_id}', offset=0, limit=N) "
        f"or GrepToolOutput(id='{raw.output_id}', pattern='...') for more)"
    )
    return DollEvent(perception=body, raw=raw)
```

- [ ] **Step 8: Add test for new perception text in tests/test_dispatcher_misc.py**

Find any test that constructs a ShellResultEvent and asserts on the rendered perception. Update assertions to:
- Check `"output_id:"` appears
- Check `"total lines:"` appears
- Check `"ReadToolOutput"` is mentioned in the hint

If no such test exists, add one.

- [ ] **Step 9: Run full suite**

```bash
uv run pytest -q
```
Expected: 100% pass.

- [ ] **Step 10: Commit**

```bash
git add src/dollos/events.py src/dollos/shell_runner.py src/dollos/dispatcher.py tests/
git commit -m "feat(tool-output): Shell results paginated via ToolOutputStore"
```

---

## Task 4: Subagent result paging

**Files:**
- Modify: `src/dollos/events.py` (SubagentResultEvent)
- Modify: `src/dollos/subagent.py`
- Modify: `src/dollos/dispatcher.py` (Subagent perception)
- Test: `tests/test_subagent.py`

- [ ] **Step 1: Add output_id field to SubagentResultEvent**

In `src/dollos/events.py`:

```python
@dataclass
class SubagentResultEvent(RawEvent):
    subagent_id: str
    task: str
    status: Literal["ok", "incomplete", "timeout", "error", "no_report"]
    summary: str   # unchanged — subagent's structured Report.summary (short by design)
    details: str   # now preview head of full details (~15 lines)
    details_output_id: str | None   # NEW; None on error before any details captured
    details_line_count: int   # NEW
    response_sink: asyncio.Queue[ServerMessage | None]
```

- [ ] **Step 2: Write failing test for subagent paging**

```python
@pytest.mark.asyncio
async def test_subagent_writes_full_details_to_store(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    events: list[SubagentResultEvent] = []

    def dispatch(e: RawEvent) -> None:
        events.append(e)

    # Stub a subagent run that produces long details
    runner = SubagentRunner(
        dispatch_fn=dispatch,
        tool_output_store=store,
        # ...other args copied from existing test fixture
    )
    long_details = "\n".join(f"finding {i}" for i in range(50))
    # Invoke runner with a stubbed Report containing long_details
    # ...(use whatever the existing test pattern does to fake a Report)
    
    # Assert
    assert len(events) == 1
    evt = events[0]
    assert evt.details_output_id is not None
    assert evt.details_line_count == 50
    assert len(evt.details.splitlines()) <= 20  # preview only
    full = store.read(evt.details_output_id, offset=0, limit=100)
    assert full.lines[0] == "finding 0"
    assert full.lines[49] == "finding 49"
```

- [ ] **Step 3: Run test, verify failure**

```bash
uv run pytest tests/test_subagent.py::test_subagent_writes_full_details_to_store -v
```
Expected: TypeError or assertion fail.

- [ ] **Step 4: Implement in subagent.py**

```python
SUBAGENT_PREVIEW_LINES = 15


class SubagentRunner:
    def __init__(
        self,
        *,
        dispatch_fn: Callable[[RawEvent], None],
        tool_output_store: "ToolOutputStore",
        # ...other existing args unchanged
    ) -> None:
        ...
        self._tool_output_store = tool_output_store
```

In the place that constructs SubagentResultEvent, change:

```python
details_full = report.details if report else ""
details_id = self._tool_output_store.write(details_full)
detail_lines = details_full.splitlines()
preview = "\n".join(detail_lines[:SUBAGENT_PREVIEW_LINES])

event = SubagentResultEvent(
    subagent_id=subagent_id,
    task=task,
    status=status,
    summary=summary,
    details=preview,
    details_output_id=details_id,
    details_line_count=len(detail_lines),
    response_sink=response_sink,
)
self._dispatch_fn(event)
```

- [ ] **Step 5: Run test, verify pass**

```bash
uv run pytest tests/test_subagent.py::test_subagent_writes_full_details_to_store -v
```
Expected: PASS.

- [ ] **Step 6: Update existing subagent tests for new signature**

Same as Shell — pass `tool_output_store=ToolOutputStore(tmp_path)`, update event assertions.

- [ ] **Step 7: Update Subagent perception in dispatcher.py**

```python
if isinstance(raw, SubagentResultEvent):
    body = (
        f"subagent finished:\n"
        f"- task: {raw.task}\n"
        f"- status: {raw.status}\n"
        f"- summary: {raw.summary}\n"
        f"- details_output_id: {raw.details_output_id}\n"
        f"- details total lines: {raw.details_line_count}\n"
        f"- details preview ({len(raw.details.splitlines())} lines):\n{raw.details}\n"
        f"(use ReadToolOutput(id='{raw.details_output_id}', offset=0, limit=N) for full details)"
    )
    return DollEvent(perception=body, raw=raw)
```

- [ ] **Step 8: Update dispatcher perception tests**

Find tests asserting on the old subagent perception text. Update.

- [ ] **Step 9: Run full suite**

```bash
uv run pytest -q
```
Expected: 100% pass.

- [ ] **Step 10: Commit**

```bash
git add src/dollos/events.py src/dollos/subagent.py src/dollos/dispatcher.py tests/
git commit -m "feat(tool-output): Subagent details paginated via ToolOutputStore"
```

---

## Task 5: ReadToolOutput tool

**Files:**
- Modify: `src/dollos/tools.py` — add pydantic tool class
- Test: `tests/test_tools.py` (or wherever existing pydantic tool tests live; check via `ls tests/`)

- [ ] **Step 1: Find existing pydantic tool tests to match patterns**

```bash
grep -rn "class.*BaseModel.*Tool\|Recall\|NoteMemory\|class Shell" src/dollos/tools.py | head -10
```

Expected: shows how Recall / NoteMemory / Shell tools are defined as BaseModel with `run(ctx)`.

- [ ] **Step 2: Write failing test for ReadToolOutput**

```python
# tests/test_tools.py or new tests/test_read_tool_output.py
@pytest.mark.asyncio
async def test_read_tool_output_returns_slice(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("\n".join(f"row {i}" for i in range(50)))
    ctx = _make_ctx(tmp_path, tool_output_store=store)

    tool = ReadToolOutput(id=output_id, offset=10, limit=5)
    result = await tool.run(ctx)

    assert "row 10" in result
    assert "row 14" in result
    assert "row 15" not in result
    assert "lines 10–15 of 50" in result   # or however formatted
```

- [ ] **Step 3: Run, verify failure**

```bash
uv run pytest tests/test_tools.py::test_read_tool_output_returns_slice -v
```
Expected: AttributeError / ImportError.

- [ ] **Step 4: Implement in tools.py**

```python
class ReadToolOutput(BaseModel):
    """Read a slice of a stored tool output by id.

    Tool outputs come from Shell / Subagent. The originating result perception
    shows the output_id and total line count; use this tool to page deeper
    than the preview.
    """

    id: str = Field(..., description="output id from a tool result perception (e.g. 'out-abc12345')")
    offset: int = Field(0, description="zero-indexed line to start at; negative counts from end")
    limit: int = Field(50, ge=1, le=500, description="max lines to return; default 50, hard cap 500")

    async def run(self, ctx: "ToolCtx") -> str:
        slice_ = ctx.tool_output_store.read(self.id, offset=self.offset, limit=self.limit)
        header = (
            f"lines {slice_.start_offset}–{slice_.end_offset} of {slice_.total_lines}:"
        )
        body = "\n".join(slice_.lines) if slice_.lines else "(empty slice)"
        return f"{header}\n{body}"
```

Register the tool in whatever the registry mechanism is (see how Recall / NoteMemory are exposed; likely via a registry list or auto-discovery).

- [ ] **Step 5: Run test, verify pass**

```bash
uv run pytest tests/test_tools.py::test_read_tool_output_returns_slice -v
```
Expected: PASS.

- [ ] **Step 6: Add tests for error cases**

```python
@pytest.mark.asyncio
async def test_read_tool_output_invalid_id(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    ctx = _make_ctx(tmp_path, tool_output_store=store)
    tool = ReadToolOutput(id="../etc/passwd", offset=0, limit=10)
    with pytest.raises(ValueError):
        await tool.run(ctx)


@pytest.mark.asyncio
async def test_read_tool_output_not_found(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    ctx = _make_ctx(tmp_path, tool_output_store=store)
    tool = ReadToolOutput(id="out-deadbeef", offset=0, limit=10)
    with pytest.raises(FileNotFoundError):
        await tool.run(ctx)
```

- [ ] **Step 7: Run, verify pass**

```bash
uv run pytest tests/test_tools.py -k "read_tool_output" -v
```
Expected: 3 PASS.

- [ ] **Step 8: Commit**

```bash
git add src/dollos/tools.py tests/test_tools.py
git commit -m "feat(tool-output): ReadToolOutput tool — page through stored output"
```

---

## Task 6: GrepToolOutput tool

**Files:**
- Modify: `src/dollos/tools.py`
- Test: `tests/test_tools.py`

- [ ] **Step 1: Write failing test**

```python
@pytest.mark.asyncio
async def test_grep_tool_output_finds_matches(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    text = "error: foo\nok\nerror: bar\nok\nERROR: baz\n"
    output_id = store.write(text)
    ctx = _make_ctx(tmp_path, tool_output_store=store)

    tool = GrepToolOutput(id=output_id, pattern=r"error:", max_matches=10)
    result = await tool.run(ctx)

    assert "line 0: error: foo" in result
    assert "line 2: error: bar" in result
    assert "ERROR" not in result   # case-sensitive by default
```

- [ ] **Step 2: Run, verify failure**

```bash
uv run pytest tests/test_tools.py::test_grep_tool_output_finds_matches -v
```
Expected: NameError / ImportError.

- [ ] **Step 3: Implement in tools.py**

```python
class GrepToolOutput(BaseModel):
    """Grep a stored tool output for a regex pattern. Returns matching lines
    with their line index, capped by max_matches.
    """

    id: str = Field(..., description="output id from a tool result perception")
    pattern: str = Field(..., description="regex pattern (Python re); case-sensitive")
    max_matches: int = Field(20, ge=1, le=200, description="max matching lines to return")

    async def run(self, ctx: "ToolCtx") -> str:
        matches = ctx.tool_output_store.grep(
            self.id, pattern=self.pattern, max_matches=self.max_matches
        )
        if not matches:
            return f"no matches for {self.pattern!r}"
        header = f"{len(matches)} match(es) for {self.pattern!r}:"
        body = "\n".join(f"line {m.line_index}: {m.line}" for m in matches)
        return f"{header}\n{body}"
```

Register in tool registry alongside ReadToolOutput.

- [ ] **Step 4: Run test, verify pass**

```bash
uv run pytest tests/test_tools.py::test_grep_tool_output_finds_matches -v
```
Expected: PASS.

- [ ] **Step 5: Add tests for no-match + invalid regex**

```python
@pytest.mark.asyncio
async def test_grep_no_match(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("hello\nworld\n")
    ctx = _make_ctx(tmp_path, tool_output_store=store)
    tool = GrepToolOutput(id=output_id, pattern=r"^nothing$", max_matches=10)
    result = await tool.run(ctx)
    assert "no matches" in result


@pytest.mark.asyncio
async def test_grep_invalid_regex_raises(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("hello\n")
    ctx = _make_ctx(tmp_path, tool_output_store=store)
    # Trailing unbalanced paren — re.error at run time
    tool = GrepToolOutput(id=output_id, pattern=r"(", max_matches=10)
    with pytest.raises(re.error):
        await tool.run(ctx)
```

- [ ] **Step 6: Run, verify pass**

```bash
uv run pytest tests/test_tools.py -k "grep_tool_output" -v
```
Expected: 3 PASS.

- [ ] **Step 7: Commit**

```bash
git add src/dollos/tools.py tests/test_tools.py
git commit -m "feat(tool-output): GrepToolOutput tool — regex search in stored output"
```

---

## Task 7: Update scaffolding template to document the tools

**Files:**
- Modify: `src/dollos/prompts/templates/scaffolding.jinja`
- Test: `tests/test_prompt_renderer.py`

- [ ] **Step 1: Read current scaffolding** to find a natural place to mention the new tools

```bash
grep -n "Shell\|Subagent\|SpawnSubagent\|ReadToolOutput\|tool" src/dollos/prompts/templates/scaffolding.jinja
```

- [ ] **Step 2: Add a Behavior bullet about tool-output paging**

In `src/dollos/prompts/templates/scaffolding.jinja`, after the existing bullet about external actions being fire-and-forget, add:

```jinja
- Shell and Subagent results land back as perceptions with an `output_id` and a short preview (first 10–15 lines). To read more, call `ReadToolOutput(id="<output_id>", offset=N, limit=K)` for paginated reads or `GrepToolOutput(id="<output_id>", pattern="...")` for regex search. The full output stays on disk; the preview is just a hint.
```

- [ ] **Step 3: Update test_prompt_renderer.py if it asserts on rendered behavior text**

Find any test that checks specific behavior bullets. If one fails because of the new bullet, update the assertion to expect the new content or just check for the new substring `"ReadToolOutput"`.

- [ ] **Step 4: Run all tests**

```bash
uv run pytest -q
```
Expected: 100% pass.

- [ ] **Step 5: Commit**

```bash
git add src/dollos/prompts/templates/scaffolding.jinja tests/test_prompt_renderer.py
git commit -m "docs(scaffolding): document ReadToolOutput / GrepToolOutput tool usage"
```

---

## Task 8: End-to-end smoke

**Files:**
- Create: `scripts/smoke_tool_output_paging.py`

This is a one-off smoke (no test), runnable manually after merging the branch.

- [ ] **Step 1: Write smoke that exercises the full path**

```python
"""End-to-end smoke for tool-output paging.

Runs a Shell command that produces 100 lines, asserts:
- ShellResultEvent has output_id and line_count=100
- Preview is ≤ 10 lines
- store.read(id, offset=50, limit=5) returns lines 50-54
- store.grep(id, "line 7", 20) finds line 70, 71, ..., 79

Print PASS / FAIL. No pytest involvement.
"""
import asyncio
import tempfile
from pathlib import Path

from dollos.shell_runner import ShellRunner
from dollos.tool_outputs import ToolOutputStore
from dollos.events import ShellResultEvent, RawEvent


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="smoke-tool-paging-"))
    store = ToolOutputStore(tmp)

    events: list[ShellResultEvent] = []

    def dispatch(e: RawEvent) -> None:
        if isinstance(e, ShellResultEvent):
            events.append(e)

    runner = ShellRunner(dispatch_fn=dispatch, tool_output_store=store)
    import asyncio as _asyncio
    sink = _asyncio.Queue()
    cmd = "for i in $(seq 1 100); do echo line $i; done"
    await runner.spawn(command=cmd, timeout_s=10.0, response_sink=sink)
    for _ in range(50):
        if events:
            break
        await asyncio.sleep(0.1)

    evt = events[0]
    print(f"event: status={evt.status}, line_count={evt.line_count}, output_id={evt.output_id}")
    print(f"preview ({len(evt.output.splitlines())} lines):\n{evt.output}")

    slice_ = store.read(evt.output_id, offset=50, limit=5)
    print(f"\nslice 50-55:\n" + "\n".join(slice_.lines))

    matches = store.grep(evt.output_id, pattern=r"line 7\d", max_matches=20)
    print(f"\ngrep 'line 7\\d': {len(matches)} matches")

    store.cleanup()
    print("\nDONE")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run smoke**

```bash
uv run python scripts/smoke_tool_output_paging.py
```
Expected: prints preview (10 lines), slice 50-55 (5 lines), 10 matches for `line 7\d`, DONE.

- [ ] **Step 3: Commit**

```bash
git add scripts/smoke_tool_output_paging.py
git commit -m "feat(tool-output): smoke script for paging end-to-end"
```

---

## Final verification

- [ ] **Step 1: Full test suite green**

```bash
uv run pytest -q
```

- [ ] **Step 2: Smoke runs end-to-end**

```bash
uv run python scripts/smoke_tool_output_paging.py
```

- [ ] **Step 3: Branch ready for merge**

Use `superpowers:finishing-a-development-branch`.
