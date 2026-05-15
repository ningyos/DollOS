"""Tests for SubagentRunner — ephemeral asyncio worker that fires
SubagentResultEvent back through the dispatcher's event queue."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel

from dollos.events import RawEvent, SubagentResultEvent
from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.llm.templates import build_qwen3_think_tool_grammar
from dollos.monitor_runner import MonitorRunner
from dollos.prompts import PromptRenderer
from dollos.shell_runner import ShellRunner
from dollos.subagent import SubagentRunner
from dollos.tool_outputs import ToolOutputStore
from dollos.tools import SUB_TOOLS, Report, Say, SpawnSubagent


# ---------- Fakes ----------


@dataclass
class _ScriptedAdapter(LLMAdapter):
    """Fake LLMAdapter for sub-cascade testing.

    `scripts` is a list-of-lists. Each outer entry is one cascade
    iteration's stream of chunks. Each call to stream_messages pops the
    next iteration's chunks. delay_per_iter optionally sleeps that long
    in EACH iteration (used to drive timeouts).
    """

    scripts: list[list[StreamChunk]] = field(default_factory=list)
    delay_per_iter: float = 0.0
    calls: list[dict] = field(default_factory=list)

    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools: list[type] | None = None,
        grammar: str | None = None,
    ) -> AsyncIterator[StreamChunk]:  # pragma: no cover
        yield StreamChunk(text="", done=True)

    async def stream_messages(
        self,
        *,
        system: str,
        messages: list[dict],
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools: list[type] | None = None,
        grammar: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append(
            {
                "system": system,
                "messages": list(messages),
                "tools": tools,
                "grammar": grammar,
            }
        )
        if not self.scripts:
            chunks = [StreamChunk(text="", done=True)]
        else:
            chunks = self.scripts.pop(0)
        if self.delay_per_iter:
            await asyncio.sleep(self.delay_per_iter)
        for c in chunks:
            yield c


class _FakeMemSearch:
    def __init__(self) -> None:
        self.indexed: list = []

    async def index_file(self, path):
        self.indexed.append(path)

    async def search(self, query: str, top_k: int = 5):
        return []


def _report_call(status: str, summary: str, details: str) -> str:
    """Render a Report tool_call XML block (single-chunk emit)."""
    import json
    body = json.dumps(
        {
            "name": "Report",
            "arguments": {
                "status": status,
                "summary": summary,
                "details": details,
            },
        },
        ensure_ascii=False,
    )
    return f"<tool_call>{body}</tool_call>"


def _make_runner(
    adapter: LLMAdapter, tmp_path: Path, dispatched: list[RawEvent] | None = None
) -> SubagentRunner:
    runner = SubagentRunner(
        adapter=adapter,
        renderer=PromptRenderer(),
        memory_root=tmp_path / "memory",
        memsearch=_FakeMemSearch(),
        transcripts_root=tmp_path / "transcripts",
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
    )
    if dispatched is not None:
        runner.set_dispatch_fn(dispatched.append)
    return runner


async def _wait_for_event(events: list[RawEvent], timeout: float = 2.0) -> RawEvent:
    """Poll the event sink until non-empty."""
    async with asyncio.timeout(timeout):
        while not events:
            await asyncio.sleep(0.01)
    return events[0]


# ---------- Tests ----------


@pytest.mark.asyncio
async def test_subagent_emits_report_fires_result_event(tmp_path: Path):
    """Happy path: subagent's first iteration emits Report; runner converts
    it into a SubagentResultEvent dispatched via dispatch_fn."""
    adapter = _ScriptedAdapter(
        scripts=[
            [
                StreamChunk(
                    text=_report_call("ok", "found 3 files", "a.md, b.md, c.md"),
                    done=False,
                ),
                StreamChunk(text="", done=True),
            ],
        ]
    )
    events: list[RawEvent] = []
    runner = _make_runner(adapter, tmp_path, events)

    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(
        sub_id="abc12345",
        task="search transcripts for coffee",
        timeout_s=30,
        response_sink=sink,
    )

    ev = await _wait_for_event(events)
    assert isinstance(ev, SubagentResultEvent)
    assert ev.subagent_id == "abc12345"
    assert ev.task == "search transcripts for coffee"
    assert ev.status == "ok"
    assert ev.summary == "found 3 files"
    assert ev.details == "a.md, b.md, c.md"
    assert ev.response_sink is sink


@pytest.mark.asyncio
async def test_subagent_timeout_fires_event_with_timeout_status(tmp_path: Path):
    adapter = _ScriptedAdapter(
        scripts=[[StreamChunk(text="", done=True)]],
        delay_per_iter=10.0,  # adapter never returns within timeout
    )
    events: list[RawEvent] = []
    runner = _make_runner(adapter, tmp_path, events)

    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(
        sub_id="t1",
        task="something slow",
        timeout_s=1,
        response_sink=sink,
    )

    ev = await _wait_for_event(events, timeout=3.0)
    assert isinstance(ev, SubagentResultEvent)
    assert ev.status == "timeout"
    assert "timeout" in ev.summary.lower()


@pytest.mark.asyncio
async def test_subagent_no_report_fires_no_report_status(tmp_path: Path):
    """Cascade ends without any tool_call → no_report status."""
    adapter = _ScriptedAdapter(
        scripts=[
            # Iteration 1: model emits no tool_call → cascade ends.
            [StreamChunk(text="(idle thinking)", done=False),
             StreamChunk(text="", done=True)],
        ]
    )
    events: list[RawEvent] = []
    runner = _make_runner(adapter, tmp_path, events)

    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(
        sub_id="nr1",
        task="do nothing",
        timeout_s=10,
        response_sink=sink,
    )

    ev = await _wait_for_event(events)
    assert ev.status == "no_report"


@pytest.mark.asyncio
async def test_subagent_runtime_error_fires_error_status(tmp_path: Path):
    """If the adapter raises, runner catches and dispatches status='error'."""

    class _BoomAdapter(LLMAdapter):
        async def stream_completion(self, **_):  # pragma: no cover
            yield StreamChunk(text="", done=True)

        async def stream_messages(self, **_):
            raise RuntimeError("kaboom")
            yield  # pragma: no cover — make this an async generator

    events: list[RawEvent] = []
    runner = _make_runner(_BoomAdapter(), tmp_path, events)

    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(
        sub_id="err1",
        task="anything",
        timeout_s=10,
        response_sink=sink,
    )

    ev = await _wait_for_event(events)
    assert ev.status == "error"
    assert "kaboom" in ev.details
    assert "RuntimeError" in ev.summary


@pytest.mark.asyncio
async def test_multiple_subagents_run_concurrently(tmp_path: Path):
    """Spawn 3 — all complete and 3 events fire."""
    adapter = _ScriptedAdapter(
        scripts=[
            [StreamChunk(text=_report_call("ok", f"s{i}", "d"), done=False),
             StreamChunk(text="", done=True)]
            for i in range(3)
        ]
    )
    events: list[RawEvent] = []
    runner = _make_runner(adapter, tmp_path, events)

    for i in range(3):
        sink: asyncio.Queue = asyncio.Queue()
        runner.spawn(
            sub_id=f"id{i}",
            task=f"task {i}",
            timeout_s=10,
            response_sink=sink,
        )

    async with asyncio.timeout(2.0):
        while len(events) < 3:
            await asyncio.sleep(0.01)
    assert len(events) == 3
    statuses = {e.status for e in events}
    assert statuses == {"ok"}
    ids = {e.subagent_id for e in events}
    assert ids == {"id0", "id1", "id2"}


@pytest.mark.asyncio
async def test_subagent_stuck_tool_aborts_with_no_report(tmp_path: Path):
    """3 consecutive same-tool failures abort cascade → no_report event."""

    # Each iteration: emit a Recall call with a query argument. ctx.memsearch.search
    # returns []; Recall returns "[no relevant memory]". Recall always
    # SUCCEEDS, so this is not a fail-cascade — let's instead emit a tool
    # call with malformed args that fails validation 3 times.
    bad_call = (
        '<tool_call>{"name":"Shell","arguments":{}}</tool_call>'
    )
    adapter = _ScriptedAdapter(
        scripts=[
            [StreamChunk(text=bad_call, done=False),
             StreamChunk(text="", done=True)],
            [StreamChunk(text=bad_call, done=False),
             StreamChunk(text="", done=True)],
            [StreamChunk(text=bad_call, done=False),
             StreamChunk(text="", done=True)],
        ]
    )
    events: list[RawEvent] = []
    runner = _make_runner(adapter, tmp_path, events)

    runner.spawn(
        sub_id="stuck1",
        task="stuck",
        timeout_s=10,
        response_sink=None,
    )

    ev = await _wait_for_event(events, timeout=2.0)
    assert ev.status == "no_report"


def test_sub_tools_excludes_say_and_spawn_subagent():
    """Subagent toolkit explicitly omits Say and SpawnSubagent; includes Report."""
    assert Say not in SUB_TOOLS
    assert SpawnSubagent not in SUB_TOOLS
    assert Report in SUB_TOOLS


def test_sub_grammar_excludes_say_and_spawn_subagent():
    """Grammar built from SUB_TOOLS does not list Say / SpawnSubagent in
    its tool-name enum or per-tool call rules."""
    g = build_qwen3_think_tool_grammar(SUB_TOOLS)
    assert '"Say"' not in g
    assert '"SpawnSubagent"' not in g
    assert "say-call" not in g
    assert "spawn-subagent-call" not in g
    # but Report IS there
    assert '"Report"' in g
    assert "report-call ::= " in g


@pytest.mark.asyncio
async def test_subagent_dispatches_via_dispatch_fn_only_after_completion(
    tmp_path: Path,
):
    """SubagentResultEvent is only dispatched when the cascade returns
    (not eagerly during streaming)."""
    completed = asyncio.Event()

    async def _slow_stream():
        await asyncio.sleep(0.1)
        completed.set()

    class _SlowAdapter(LLMAdapter):
        async def stream_completion(self, **_):  # pragma: no cover
            yield StreamChunk(text="", done=True)

        async def stream_messages(self, **_):
            await _slow_stream()
            yield StreamChunk(
                text=_report_call("ok", "done", "x"),
                done=False,
            )
            yield StreamChunk(text="", done=True)

    events: list[RawEvent] = []
    runner = _make_runner(_SlowAdapter(), tmp_path, events)
    runner.spawn(sub_id="s1", task="x", timeout_s=5, response_sink=None)

    # Right after spawn — no event yet.
    assert events == []
    await asyncio.wait_for(completed.wait(), timeout=1.0)
    # Give the runner a tick to dispatch.
    await asyncio.sleep(0.05)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_subagent_runner_stop_cancels_inflight(tmp_path: Path):
    """stop() cancels in-flight tasks; no result event is dispatched for them."""

    class _HangAdapter(LLMAdapter):
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def stream_completion(self, **_):  # pragma: no cover
            yield StreamChunk(text="", done=True)

        async def stream_messages(self, **_):
            self.entered.set()
            await asyncio.Event().wait()
            yield StreamChunk(text="", done=True)  # pragma: no cover

    adapter = _HangAdapter()
    events: list[RawEvent] = []
    runner = _make_runner(adapter, tmp_path, events)
    runner.spawn(sub_id="hang1", task="x", timeout_s=60, response_sink=None)
    await asyncio.wait_for(adapter.entered.wait(), timeout=1.0)
    await asyncio.wait_for(runner.stop(), timeout=1.0)
    # No event dispatched (cancellation skips the dispatch path).
    assert events == []


@pytest.mark.asyncio
async def test_subagent_uses_subagent_scaffolding(tmp_path: Path):
    """First (and only) adapter call's system prompt is the subagent
    scaffolding — not Doll's main scaffolding (no `# Identity`)."""
    adapter = _ScriptedAdapter(
        scripts=[
            [StreamChunk(text=_report_call("ok", "x", "y"), done=False),
             StreamChunk(text="", done=True)],
        ]
    )
    events: list[RawEvent] = []
    runner = _make_runner(adapter, tmp_path, events)
    runner.spawn(sub_id="s1", task="hello", timeout_s=10, response_sink=None)
    await _wait_for_event(events)

    assert len(adapter.calls) == 1
    system = adapter.calls[0]["system"]
    assert "subagent" in system
    assert "Report" in system
    assert "# Identity" not in system  # no character block
    assert "[Memory context]" not in system  # no memory block
    # The user message is the raw task, no [Memory context] / [Message] wrapping.
    msgs = adapter.calls[0]["messages"]
    assert msgs[0] == {"role": "user", "content": "hello"}


# ----- ShellRunner on sub-cascade ctx -----


@pytest.mark.asyncio
async def test_subagent_ctx_has_shell_runner(tmp_path: Path):
    """SubagentRunner forwards its shell_runner into the sub-cascade
    ToolCtx so subagents can use Shell."""
    from dollos.tools import SUB_TOOLS, ToolCtx

    captured: list[ToolCtx] = []

    class _CaptureTool(BaseModel):
        token: str = "x"

        async def run(self, ctx) -> str:
            captured.append(ctx)
            return None  # side-effect tool, ends sub-cascade naturally

    SUB_TOOLS_orig = list(SUB_TOOLS)
    SUB_TOOLS.clear()
    SUB_TOOLS.extend([_CaptureTool])
    try:
        adapter = _ScriptedAdapter(
            scripts=[
                [
                    StreamChunk(
                        text='<tool_call>{"name":"_CaptureTool","arguments":{"token":"x"}}</tool_call>',
                        done=False,
                    ),
                    StreamChunk(text="", done=True),
                ],
            ]
        )
        events: list[RawEvent] = []
        shell_runner = ShellRunner(cwd=tmp_path, tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"))
        runner = SubagentRunner(
            adapter=adapter,
            renderer=PromptRenderer(),
            memory_root=tmp_path / "memory",
            memsearch=_FakeMemSearch(),
            transcripts_root=tmp_path / "transcripts",
            shell_runner=shell_runner,
            tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        )
        runner.set_dispatch_fn(events.append)
        sink: asyncio.Queue = asyncio.Queue()
        runner.spawn(
            sub_id="reg1",
            task="capture ctx",
            timeout_s=10,
            response_sink=sink,
        )
        await _wait_for_event(events, timeout=3.0)

        assert captured, "tool was not invoked inside sub-cascade"
        assert captured[0].shell_runner is shell_runner
    finally:
        SUB_TOOLS.clear()
        SUB_TOOLS.extend(SUB_TOOLS_orig)


# ----- MonitorRunner on sub-cascade ctx -----


@pytest.mark.asyncio
async def test_subagent_ctx_has_monitor_runner(tmp_path: Path):
    """SubagentRunner forwards its monitor_runner into the sub-cascade
    ToolCtx so subagents can use SpawnMonitor/RemoveMonitor."""
    from dollos.tools import SUB_TOOLS, ToolCtx

    captured: list[ToolCtx] = []

    class _CaptureTool(BaseModel):
        token: str = "x"

        async def run(self, ctx) -> str:
            captured.append(ctx)
            return None  # side-effect tool, ends sub-cascade naturally

    SUB_TOOLS_orig = list(SUB_TOOLS)
    SUB_TOOLS.clear()
    SUB_TOOLS.extend([_CaptureTool])
    try:
        adapter = _ScriptedAdapter(
            scripts=[
                [
                    StreamChunk(
                        text='<tool_call>{"name":"_CaptureTool","arguments":{"token":"x"}}</tool_call>',
                        done=False,
                    ),
                    StreamChunk(text="", done=True),
                ],
            ]
        )
        events: list[RawEvent] = []
        monitor_runner = MonitorRunner(cwd=tmp_path)
        runner = SubagentRunner(
            adapter=adapter,
            renderer=PromptRenderer(),
            memory_root=tmp_path / "memory",
            memsearch=_FakeMemSearch(),
            transcripts_root=tmp_path / "transcripts",
            monitor_runner=monitor_runner,
            tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        )
        runner.set_dispatch_fn(events.append)
        sink: asyncio.Queue = asyncio.Queue()
        runner.spawn(
            sub_id="reg2",
            task="capture ctx",
            timeout_s=10,
            response_sink=sink,
        )
        await _wait_for_event(events, timeout=3.0)

        assert captured, "tool was not invoked inside sub-cascade"
        assert captured[0].monitor_runner is monitor_runner
    finally:
        SUB_TOOLS.clear()
        SUB_TOOLS.extend(SUB_TOOLS_orig)
