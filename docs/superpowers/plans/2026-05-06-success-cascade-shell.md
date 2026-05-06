# Success-cascade + Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend step 7 fail-only cascade to a unified success+fail cascade. `Tool.run` signature becomes `-> str | None` (None = no cascade, str = cascade with content). Add `Shell` returning tool that runs bash subprocess and feeds stdout+stderr back to Doll's next perception.

**Architecture:** Rename step 7's `ToolCallFailure` → `ToolResult` with a `success: bool` field. `_dispatch_tool_call` returns `ToolResult | None`: None when the tool's `run()` returned None (side-effect tool), else a result. Existing tools (Say/NoteMemory/WriteDiary) keep their behavior but get explicit `-> None`. New `Shell(BaseModel)` tool runs `bash -c <command>` via `asyncio.to_thread(subprocess.run, ...)` with cwd=`settings.data.root`, default 30s timeout (Doll-overridable, max 300s), combined stdout+stderr, 8000-char head/tail truncation. `_format_results_perception` formats both success and fail results into Doll's next perception narrative. `_respond` while-loop continues until results list is empty.

**Tech Stack:** Python 3.12+, `asyncio`, `subprocess`, `pydantic`, `pytest` + `pytest-asyncio`. No new external deps.

**Spec:** `docs/superpowers/specs/2026-05-06-success-cascade-shell-design.md`

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `src/dollos/dispatcher.py` | modify | `ToolCallFailure` → `ToolResult` (add `success`); `_dispatch_tool_call` returns `ToolResult \| None`; `_format_fail_perception` → `_format_results_perception`; `_respond` collects ToolResult |
| `src/dollos/tools.py` | modify | `Say.run` / `NoteMemory.run` / `WriteDiary.run` add explicit `-> None`; new `Shell` tool with `_truncate` helper; constants `SHELL_DEFAULT_TIMEOUT_S`, `SHELL_MAX_TIMEOUT_S`, `SHELL_OUTPUT_MAX_CHARS` |
| `tests/test_dispatcher.py` | modify | Update existing tests for `ToolResult` rename + `success=False`; add success-cascade tests |
| `tests/test_tools.py` | extend | New `Shell` tests; existing tools' return-type assertions |
| `docs/roadmap.md` | modify | Mark step 9 merged; point to step 10 |
| `CLAUDE.md` | modify | Same |

---

## Task 1: Rename `ToolCallFailure` → `ToolResult` (no behavior change)

**Files:**
- Modify: `src/dollos/dispatcher.py`
- Modify: `tests/test_dispatcher.py`

Pure rename + add `success: bool = False` field. All call sites pass `success=False` so behavior is identical to step 7. Sets up the data structure for Task 2's success path.

### Step 1: Write failing test (RED)

- [ ] Append to `tests/test_dispatcher.py`:

```python
from dollos.dispatcher import ToolResult


def test_tool_result_dataclass_fields():
    r = ToolResult(tool_name="X", success=False, detail="boom")
    assert r.tool_name == "X"
    assert r.success is False
    assert r.detail == "boom"


def test_tool_result_success_field_defaults_to_required():
    """success must be explicit (no default) — caller intent should be visible."""
    import dataclasses
    fields = {f.name: f for f in dataclasses.fields(ToolResult)}
    assert "success" in fields
    # success has no default → field.default is dataclasses.MISSING
    assert fields["success"].default is dataclasses.MISSING
```

- [ ] Update existing tests in `tests/test_dispatcher.py` that import `ToolCallFailure`:
  - Replace `from dollos.dispatcher import ToolCallFailure` → `from dollos.dispatcher import ToolResult`
  - Replace `isinstance(fail, ToolCallFailure)` → `isinstance(fail, ToolResult) and not fail.success`
  - Replace `ToolCallFailure(tool_name=..., error=...)` constructor → `ToolResult(tool_name=..., success=False, detail=...)` (`error` field renamed to `detail`)

(Find all `ToolCallFailure` references in test files. Currently 5 direct-test cases + cascade tests reference it.)

- [ ] Run: `uv run pytest tests/test_dispatcher.py -q`
- [ ] Expected: failures (`ToolResult` doesn't exist; renamed-import failures).

### Step 2: Implement (GREEN)

- [ ] Edit `src/dollos/dispatcher.py`. Replace the `ToolCallFailure` dataclass:

```python
@dataclass
class ToolResult:
    """Tool execution result. Internal cascade primitive (not a RawEvent).

    success=False: mechanical fail (validation / unknown / runtime exception).
    success=True:  ran cleanly. detail = the str returned by Tool.run().
                   May be empty string (Tool ran but had no content to return).

    Failures always cascade (Doll should fix). Successes cascade iff
    Tool.run() returned a str (not None) — i.e., the tool author opted in.
    """

    tool_name: str
    success: bool
    detail: str
```

- [ ] In `_dispatch_tool_call`, change the return type and constructor calls. Find all 4 places that return `ToolCallFailure(tool_name=..., error=...)` and change to `ToolResult(tool_name=..., success=False, detail=...)`. The signature changes from `-> ToolCallFailure | None` to `-> ToolResult | None`.

- [ ] In `_format_fail_perception` (rename to `_format_results_perception` in Task 2), the references to `f.error` need to become `f.detail`. For Task 1, just rename the field reference: `f.error` → `f.detail`.

- [ ] Verify no remaining `ToolCallFailure` references in `src/dollos/`:
  ```bash
  grep -rn "ToolCallFailure" src/dollos/
  ```
  Expected: no output.

### Step 3: Run tests (GREEN)

- [ ] Run: `uv run pytest tests/test_dispatcher.py -q`
- [ ] Expected: all dispatcher tests pass.
- [ ] Run: `uv run pytest -q`
- [ ] Expected: full suite green (146 passed).

### Step 4: Lint

- [ ] Run: `uv run ruff check src/dollos/dispatcher.py tests/test_dispatcher.py`
- [ ] Expected: clean.

### Step 5: Commit

- [ ] Run:

```bash
git add src/dollos/dispatcher.py tests/test_dispatcher.py
git commit -m "$(cat <<'EOF'
refactor(dispatcher): rename ToolCallFailure -> ToolResult, add success field

Pure rename + add success: bool field. All call sites pass success=False
so behavior is identical to step 7 fail-only cascade. Sets up data
structure for the success-cascade path in next task.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Tool.run signature `-> str | None` + success-cascade in dispatcher

**Files:**
- Modify: `src/dollos/dispatcher.py`
- Modify: `src/dollos/tools.py`
- Modify: `tests/test_dispatcher.py`
- Modify: `tests/test_tools.py`

`_dispatch_tool_call` distinguishes None (no cascade) from str (success cascade). `_respond` collects all results. `_format_results_perception` handles success/fail/empty cases. Existing tools get explicit `-> None`.

### Step 1: Write failing tests (RED)

- [ ] Append to `tests/test_dispatcher.py`:

```python
@pytest.mark.asyncio
async def test_dispatch_tool_call_returns_none_when_tool_run_returns_none(tmp_path):
    """Side-effect tool (Say) returning None → _dispatch_tool_call returns None (no cascade)."""
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=_FakeAdapter(chunks=[]),
        inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), character_profile="x",
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_tool_ctx(sink, tmp_path, ms)

    result = await disp._dispatch_tool_call(
        {"name": "Say", "arguments": {"text": "hi"}}, ctx
    )

    assert result is None


@pytest.mark.asyncio
async def test_dispatch_tool_call_returns_success_result_when_tool_returns_str(tmp_path):
    """Returning tool returning str → ToolResult(success=True, detail=str)."""

    class _ReturningTool(BaseModel):
        text: str
        async def run(self, ctx) -> str:
            return f"echoed: {self.text}"

    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=_FakeAdapter(chunks=[]),
        inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), character_profile="x",
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    disp._tools_by_name["_ReturningTool"] = _ReturningTool
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_tool_ctx(sink, tmp_path, ms)

    result = await disp._dispatch_tool_call(
        {"name": "_ReturningTool", "arguments": {"text": "hi"}}, ctx
    )

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.detail == "echoed: hi"


@pytest.mark.asyncio
async def test_dispatch_tool_call_returns_success_result_with_empty_str(tmp_path):
    """Returning tool returning empty str → ToolResult(success=True, detail='')."""

    class _EmptyReturningTool(BaseModel):
        async def run(self, ctx) -> str:
            return ""

    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=_FakeAdapter(chunks=[]),
        inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), character_profile="x",
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    disp._tools_by_name["_EmptyReturningTool"] = _EmptyReturningTool
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_tool_ctx(sink, tmp_path, ms)

    result = await disp._dispatch_tool_call(
        {"name": "_EmptyReturningTool", "arguments": {}}, ctx
    )

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.detail == ""


@pytest.mark.asyncio
async def test_respond_cascades_success_with_returning_tool(tmp_path: Path):
    """Round 1: returning tool fires → cascade → Round 2: Say wraps up."""

    class _RoundedFakeAdapter:
        def __init__(self, rounds):
            self._rounds = rounds
            self.calls: list[dict] = []

        async def stream_completion(
            self, *, system, user, prefill, stop=None,
            max_tokens=1024, tools=None,
        ):
            idx = len(self.calls)
            self.calls.append(
                {"system": system, "user": user, "prefill": prefill,
                 "tools": tools}
            )
            for c in self._rounds[idx]:
                yield c

    class _Echo(BaseModel):
        text: str
        async def run(self, ctx) -> str:
            return f"got: {self.text}"

    rounds = [
        [
            StreamChunk(
                text='<tool_call>{"name":"_Echo","arguments":{"text":"hi"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ],
        [
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"done"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ],
    ]
    adapter = _RoundedFakeAdapter(rounds)
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=["", ""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), character_profile="x",
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    disp._tools_by_name["_Echo"] = _Echo

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    items = []
    while True:
        m = await sink.get()
        if m is None:
            break
        items.append(m)

    assert len(adapter.calls) == 2
    second_user = adapter.calls[1]["user"]
    assert "_Echo" in second_user
    assert "成功" in second_user
    assert "got: hi" in second_user
    assert "第 1 次重試" in second_user
    text_chunks = [m for m in items if isinstance(m, TextChunk)]
    assert any(m.text == "done" for m in text_chunks)


@pytest.mark.asyncio
async def test_respond_cascades_success_with_empty_str_perception(tmp_path: Path):
    """Empty-string success cascade: perception says '成功，無輸出'."""

    class _RoundedFakeAdapter:
        def __init__(self, rounds):
            self._rounds = rounds
            self.calls: list[dict] = []

        async def stream_completion(
            self, *, system, user, prefill, stop=None,
            max_tokens=1024, tools=None,
        ):
            idx = len(self.calls)
            self.calls.append(
                {"system": system, "user": user, "prefill": prefill,
                 "tools": tools}
            )
            for c in self._rounds[idx]:
                yield c

    class _Empty(BaseModel):
        async def run(self, ctx) -> str:
            return ""

    rounds = [
        [
            StreamChunk(
                text='<tool_call>{"name":"_Empty","arguments":{}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ],
        [
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ],
    ]
    adapter = _RoundedFakeAdapter(rounds)
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=["", ""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), character_profile="x",
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    disp._tools_by_name["_Empty"] = _Empty

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    second_user = adapter.calls[1]["user"]
    assert "_Empty" in second_user
    assert "成功" in second_user
    assert "無輸出" in second_user


@pytest.mark.asyncio
async def test_respond_no_cascade_when_only_none_returning_tools(tmp_path: Path):
    """Say (returns None) → no cascade → turn ends after one round."""
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
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), character_profile="x",
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    assert len(adapter.calls) == 1   # no cascade
```

(Add `from pydantic import BaseModel` to test imports if not already present.)

- [ ] Run: `uv run pytest tests/test_dispatcher.py -q`
- [ ] Expected: 6 new tests fail (current `_dispatch_tool_call` doesn't differentiate None/str returns; doesn't return success cases).

### Step 2: Implement (GREEN) — `tools.py` explicit `-> None`

- [ ] Edit `src/dollos/tools.py`. Add explicit `-> None` to existing run methods:

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

- [ ] Same for `NoteMemory` and `WriteDiary`: change `async def run(self, ctx: ToolCtx)` → `async def run(self, ctx: ToolCtx) -> None` (just the type annotation; keep `await` and body unchanged).

### Step 3: Implement (GREEN) — `dispatcher.py` cascade

- [ ] Edit `src/dollos/dispatcher.py`. Replace `_dispatch_tool_call`:

```python
async def _dispatch_tool_call(
    self, call: dict, ctx: ToolCtx
) -> ToolResult | None:
    """Execute a tool call. Returns ToolResult if cascade-worthy, None otherwise.

    Returns None when:
      - tool.run() returned None (side-effect tool, no cascade)
    Returns ToolResult when:
      - validation/unknown error (success=False, error in detail)
      - runtime exception (success=False, error in detail) — also pushes ErrorMsg to sink
      - tool.run() returned str (success=True, str in detail; may be empty)
    """
    name = call.get("name")
    if not isinstance(name, str):
        return ToolResult(
            tool_name=str(name), success=False,
            detail="missing or non-string 'name' field in tool_call",
        )
    tool_cls = self._tools_by_name.get(name)
    if tool_cls is None:
        logger.warning("unknown tool: %r", name)
        return ToolResult(tool_name=name, success=False, detail="unknown tool")
    try:
        tool = tool_cls.model_validate(call.get("arguments", {}))
    except ValidationError as e:
        logger.warning("tool args validation failed for %s: %s", name, e)
        return ToolResult(
            tool_name=name, success=False, detail=f"args validation: {e}"
        )
    try:
        returned = await tool.run(ctx)
    except Exception as e:
        logger.exception("tool %s raised", name)
        ctx.sink.put_nowait(ErrorMsg(message=f"tool {name} error: {e}"))
        return ToolResult(
            tool_name=name, success=False, detail=f"runtime error: {e}"
        )
    if returned is None:
        return None
    return ToolResult(tool_name=name, success=True, detail=returned)
```

- [ ] Replace `_respond`'s while-loop body — replace `fails: list[ToolResult] = []` collection variable name with `results: list[ToolResult] = []`, and change `if not fails:` to `if not results:`. Pattern: rename `fail` → `result` and `fails` → `results` consistently in `_respond`.

- [ ] Replace `_format_fail_perception` with `_format_results_perception`:

```python
@staticmethod
def _format_results_perception(
    results: list[ToolResult], iteration: int
) -> str:
    lines = []
    for r in results:
        if r.success:
            if r.detail:
                lines.append(
                    f"你 call 了 {r.tool_name} tool 成功，回傳：\n{r.detail}"
                )
            else:
                lines.append(
                    f"你 call 了 {r.tool_name} tool 成功，無輸出。"
                )
        else:
            lines.append(f"你 call 了 {r.tool_name} tool 失敗：{r.detail}")
    lines.append(f"（這是 thread 的第 {iteration} 次重試）")
    return "\n\n".join(lines)
```

- [ ] In `_respond`, the call site that builds the next perception:
```python
doll_event = DollEvent(
    perception=self._format_results_perception(results, iteration),
    raw=doll_event.raw,
)
```

- [ ] Search for any remaining `_format_fail_perception` references and replace.

### Step 4: Run tests (GREEN)

- [ ] Run: `uv run pytest tests/test_dispatcher.py -q`
- [ ] Expected: all dispatcher tests pass.
- [ ] Run: `uv run pytest -q`
- [ ] Expected: full suite green.

### Step 5: Lint

- [ ] Run: `uv run ruff check src/dollos/dispatcher.py src/dollos/tools.py tests/test_dispatcher.py tests/test_tools.py`
- [ ] Expected: clean.

### Step 6: Commit

- [ ] Run:

```bash
git add src/dollos/dispatcher.py src/dollos/tools.py tests/test_dispatcher.py tests/test_tools.py
git commit -m "$(cat <<'EOF'
feat(dispatcher,tools): success-cascade for returning tools

Tool.run signature becomes -> str | None. None = side-effect tool, no
cascade. str = cascade with content (empty str cascades with "成功，無輸出"
perception). _dispatch_tool_call returns ToolResult | None accordingly.
_respond collects results; non-empty list triggers next-iteration cascade.
_format_fail_perception → _format_results_perception handles success/fail/
empty uniformly. Existing tools (Say/NoteMemory/WriteDiary) get explicit
-> None type annotation; behavior unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `Shell` tool

**Files:**
- Modify: `src/dollos/tools.py`
- Modify: `tests/test_tools.py`

Add the `Shell` pydantic tool plus `_truncate` helper. First returning tool to use the cascade infrastructure.

### Step 1: Write failing tests (RED)

- [ ] Append to `tests/test_tools.py`:

```python
from dollos.tools import (
    SHELL_DEFAULT_TIMEOUT_S,
    SHELL_MAX_TIMEOUT_S,
    SHELL_OUTPUT_MAX_CHARS,
    Shell,
    _truncate,
)


def test_shell_in_tools_list():
    from dollos.tools import TOOLS
    assert Shell in TOOLS


def test_shell_schema_has_command_and_timeout():
    schema = Shell.model_json_schema()
    assert "command" in schema["properties"]
    assert schema["properties"]["command"]["type"] == "string"
    assert "timeout_s" in schema["properties"]


def test_shell_timeout_validation_lower_bound():
    with pytest.raises(Exception):  # pydantic ValidationError
        Shell(command="echo", timeout_s=0)


def test_shell_timeout_validation_upper_bound():
    with pytest.raises(Exception):
        Shell(command="echo", timeout_s=SHELL_MAX_TIMEOUT_S + 1)


def test_shell_timeout_default():
    s = Shell(command="echo")
    assert s.timeout_s == SHELL_DEFAULT_TIMEOUT_S


def test_truncate_under_cap_returns_unchanged():
    assert _truncate("hello", 100) == "hello"


def test_truncate_over_cap_inserts_marker():
    long = "a" * 500
    out = _truncate(long, 100)
    assert "[truncated 400 chars]" in out
    assert out.startswith("a" * 50)
    assert out.endswith("a" * 50)
    assert len(out) < len(long)


@pytest.mark.asyncio
async def test_shell_run_echo_returns_stdout(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path / "memory",
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    out = await Shell(command="echo hi").run(ctx)
    assert "[exit 0]" in out
    assert "hi" in out


@pytest.mark.asyncio
async def test_shell_run_nonzero_exit_still_returns_str(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path / "memory",
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    out = await Shell(command="false").run(ctx)
    assert "[exit 1]" in out


@pytest.mark.asyncio
async def test_shell_cwd_is_data_root(tmp_path):
    """Shell runs with cwd = ctx.memory_root.parent (i.e. data/)."""
    data_root = tmp_path / "data"
    memory_root = data_root / "memory"
    memory_root.mkdir(parents=True)
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    ctx = ToolCtx(
        sink=sink,
        memory_root=memory_root,
        memsearch=ms,
        transcripts_root=data_root / "transcripts",
    )
    out = await Shell(command="pwd").run(ctx)
    assert str(data_root.resolve()) in out


@pytest.mark.asyncio
async def test_shell_combines_stdout_and_stderr(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path / "memory",
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    # bash -c emits both
    out = await Shell(
        command="echo to_stdout; echo to_stderr 1>&2"
    ).run(ctx)
    assert "to_stdout" in out
    assert "to_stderr" in out


@pytest.mark.asyncio
async def test_shell_timeout_returns_message_not_exception(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path / "memory",
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    out = await Shell(command="sleep 5", timeout_s=1).run(ctx)
    assert "shell timeout" in out
    assert "1s" in out


@pytest.mark.asyncio
async def test_shell_truncates_long_output(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path / "memory",
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    # Generate ~16k of output (more than 8000-char cap)
    out = await Shell(
        command=f"yes hello | head -c {SHELL_OUTPUT_MAX_CHARS * 2}"
    ).run(ctx)
    assert "[truncated" in out
```

(`memory_root` is set to a not-yet-existing path in some tests; that's fine — `Shell.run` only uses `cwd = memory_root.parent`. Make sure `data_root`/`memory_root.parent` exists where the test asserts pwd output.)

- [ ] Run: `uv run pytest tests/test_tools.py -q`
- [ ] Expected: 12 new tests fail (Shell doesn't exist yet).

### Step 2: Implement (GREEN)

- [ ] Edit `src/dollos/tools.py`. Add to imports near top:

```python
import asyncio
import subprocess
```

(`asyncio` should already be imported. `subprocess` is new.)

- [ ] Add module-level constants near top (after existing imports, before classes):

```python
SHELL_DEFAULT_TIMEOUT_S = 30
SHELL_MAX_TIMEOUT_S = 300
SHELL_OUTPUT_MAX_CHARS = 8000


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    half = cap // 2
    head = text[:half]
    tail = text[-half:]
    dropped = len(text) - 2 * half
    return f"{head}\n...[truncated {dropped} chars]...\n{tail}"
```

- [ ] Add the `Shell` class. Place after `WriteDiary`:

```python
class Shell(BaseModel):
    """Execute a shell command. Returns combined stdout+stderr.

    Subprocess runs with the daemon's user permissions. Working directory
    starts at settings.data.root each call (cd does NOT persist between
    calls — each Shell invocation is a fresh subprocess).

    Use this for any system inspection (ls, cat, find, ps, ...) or any
    command-line task. Output is truncated to 8000 chars total if longer.
    """

    command: str = Field(
        description="The shell command to run (will be passed to bash -c)."
    )
    timeout_s: int = Field(
        default=SHELL_DEFAULT_TIMEOUT_S,
        ge=1,
        le=SHELL_MAX_TIMEOUT_S,
        description=(
            f"Seconds before timeout. Default {SHELL_DEFAULT_TIMEOUT_S}, "
            f"max {SHELL_MAX_TIMEOUT_S}."
        ),
    )

    async def run(self, ctx: ToolCtx) -> str:
        cwd = ctx.memory_root.parent
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["bash", "-c", self.command],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return f"[shell timeout after {self.timeout_s}s]"
        combined = proc.stdout
        if proc.stderr:
            combined += proc.stderr
        prefix = f"[exit {proc.returncode}]\n"
        body = _truncate(combined, SHELL_OUTPUT_MAX_CHARS)
        return prefix + body
```

- [ ] Update `TOOLS`:

```python
TOOLS: list[type[BaseModel]] = [Say, NoteMemory, WriteDiary, Shell]
```

### Step 3: Run tests (GREEN)

- [ ] Run: `uv run pytest tests/test_tools.py -q`
- [ ] Expected: all tool tests pass (existing + 12 new).
- [ ] Run: `uv run pytest -q`
- [ ] Expected: full suite green.

### Step 4: Lint

- [ ] Run: `uv run ruff check src/dollos/tools.py tests/test_tools.py`
- [ ] Expected: clean.

### Step 5: Commit

- [ ] Run:

```bash
git add src/dollos/tools.py tests/test_tools.py
git commit -m "$(cat <<'EOF'
feat(tools): Shell pydantic tool — bash subprocess with cascade

First returning tool. subprocess.run via asyncio.to_thread, cwd =
ctx.memory_root.parent (=data/), default 30s timeout (Doll-overridable
1-300s), combined stdout+stderr, "[exit N]" prefix, 8000-char head/tail
truncation marker. TimeoutExpired returns "[shell timeout after Ns]"
string (not raised) — tool-level success.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Manual smoke test

**Files:** None (validation only).

Verify Shell + cascade end-to-end against real models.

### Step 1: Verify servers + start daemon

- [ ] `curl -s -o /dev/null -w "8001=%{http_code} 8003=%{http_code}\n" http://localhost:8001/health http://localhost:8003/health`
- [ ] Expected: both `200`.
- [ ] From worktree root:
  ```bash
  cd /home/progcat/Projects/DollOS/.worktrees/success-cascade-shell
  mkdir -p data/memory/shared data/memory/transcripts
  rm -f /tmp/dollos.log
  uv run python -m dollos --config config.toml > /tmp/dollos.log 2>&1 &
  sleep 6
  tail -3 /tmp/dollos.log
  ```
- [ ] Expected: "memsearch indexed" + "ipc server listening".

### Step 2: Drive Shell turn

- [ ] `uv run python experiments/ws_client.py "幫我看 data/memory/transcripts/ 有什麼檔案"`
- [ ] Expected ws_client output: clean Doll response describing the directory contents (or saying it's empty). No `<tool_call>` markers leaked. No `[exit N]` markers in user-facing text.
- [ ] In `/tmp/dollos.log`, expect TWO big-model calls (port 8001) — first round emits Shell tool_call, second round (after cascade) emits Say tool_call.

### Step 3: Verify cross-cascade flow

- [ ] `cat /tmp/dollos.log | grep "8001"` → should see ≥2 POST entries for the turn (cascade re-invoke).
- [ ] `cat data/memory/transcripts/$(date +%Y-%m-%d).md` → final Doll utterance written via Say.

### Step 4: Stress test (optional)

- [ ] `uv run python experiments/ws_client.py "用 shell 執行 'echo hello world' 然後告訴我結果"`
- [ ] Expected: Doll mentions "hello world" in her response, having seen Shell output via cascade.

### Step 5: Stop daemon

- [ ] `pkill -f "python -m dollos"`

### Step 6: Document outcomes

- [ ] If Doll calls Shell + acts on result → Task 4 done.
- [ ] If Doll never calls Shell (model judgment) → record observation; don't block merge. Unit tests cover the cascade mechanic.
- [ ] If output leaks `<tool_call>` markers → record + investigate.

No commit needed.

---

## Task 5: Roadmap + CLAUDE.md sync

**Files:**
- Modify: `docs/roadmap.md`
- Modify: `CLAUDE.md`

### Step 1: Update `docs/roadmap.md`

- [ ] In `## 已完成` table, append:

```markdown
| Roadmap step 9 — Success-cascade + Shell | Merged |
```

- [ ] In `### 9. Subagent` heading, replace body with a "**Re-cut**" notice (the original step 9 plan is deferred — subagent didn't ship; what shipped is success-cascade + Shell):

```
**Re-cut**: 原 roadmap step 9 為 Subagent。實際排程改：先做 success-cascade + Shell（讓 Doll 透過 shell 操控環境，並把 cascade 從 fail-only 升級成 success+fail unified）；Subagent 留到之後。

Step 9 minimal scope: Tool.run 簽名 `-> str | None`（None = side-effect tool 不 cascade，str = cascade with content）。`ToolCallFailure` 升級成 `ToolResult(tool_name, success, detail)`，success/fail 共用 cascade 路徑。新 `Shell` returning tool（fresh subprocess via asyncio.to_thread，cwd=`data/`，default 30s/max 300s timeout，stdout+stderr 合併，8000-char head/tail truncation）。trust-only（無 permission gate / 無 sandbox）。

**Demo**：Doll 透過 Shell 執行命令、看結果、接續講話；cascade 同 turn 多輪正常。

下個 step 是 step 10（Skills system — entry/body 分離 + InvokeSkill returning tool）。
```

### Step 2: Update `CLAUDE.md`

- [ ] In "已完成" plan table, append:

```markdown
| Roadmap step 9 — Success-cascade + Shell | Merged |
```

- [ ] Replace "下一個" paragraph with step-10 brief:

```
**Roadmap step 10 — Skills system**：Anthropic-skill 風格 markdown 檔，但放進 memory 體系——entry 檔（短 description + frontmatter）由 memsearch 索引、進 RECALL；body 檔不索引、由 `InvokeSkill(name)` returning tool 主動載入（吃 step 9 的 success-cascade）。Doll 用 Shell 寫新 skill。**並行**：reflex 仍待 wake gating 後才有意義。完整 roadmap：`docs/roadmap.md`。
```

### Step 3: Verify

- [ ] `uv run pytest -q` — green.
- [ ] `uv run ruff check src/dollos tests` — clean (modulo pre-existing).

### Step 4: Commit

- [ ] Run:

```bash
git add docs/roadmap.md CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: mark roadmap step 9 (success-cascade + Shell) merged, point to step 10

Step 9 re-cut: shipped success-cascade + Shell tool. Original roadmap
step 9 (Subagent) deferred. Step 10 = Skills system (entry/body split
+ InvokeSkill, riding the new success-cascade infrastructure).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Done definition

- [ ] All 5 tasks committed on branch `success-cascade-shell`.
- [ ] `uv run pytest -q` green.
- [ ] `uv run ruff check src/dollos tests` clean.
- [ ] Smoke test (Task 4): Doll calls Shell + cascade triggers second-round Say.
- [ ] Roadmap + CLAUDE.md updated.
- [ ] Ready for `superpowers:finishing-a-development-branch`.
