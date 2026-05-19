"""Tests for SubagentRunner — ephemeral asyncio worker that fires
ToolResultArrived Perception back through the PerceptionQueue."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel

from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.llm.templates import build_qwen3_think_tool_grammar
from dollos.mind.mind_state import Perception
from dollos.mind.perception_queue import PerceptionQueue
from dollos.monitor_runner import MonitorRunner
from dollos.prompts import PromptRenderer
from dollos.shell_runner import ShellRunner
from dollos.subagent import SubagentRunner
from dollos.tool_outputs import ToolOutputStore
from dollos.tools import SUB_TOOLS, Report, SpawnSubagent


# ---------- Fakes ----------


@dataclass
class _ScriptedAdapter(LLMAdapter):
    """Fake LLMAdapter for sub-cascade testing."""

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
    adapter: LLMAdapter, tmp_path: Path, queue: PerceptionQueue | None = None
) -> tuple[SubagentRunner, PerceptionQueue]:
    q = queue if queue is not None else PerceptionQueue()
    runner = SubagentRunner(
        adapter=adapter,
        renderer=PromptRenderer(),
        memory_root=tmp_path / "memory",
        memsearch=_FakeMemSearch(),
        transcripts_root=tmp_path / "transcripts",
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        perception_queue=q,
    )
    return runner, q


async def _wait_for_tool_result(queue: PerceptionQueue, timeout: float = 2.0) -> Perception:
    """Poll until a ToolResultArrived perception arrives, return first."""
    async with asyncio.timeout(timeout):
        while True:
            perceptions = await queue.drain(timeout_s=0.01)
            for p in perceptions:
                if p.kind == "ToolResultArrived":
                    return p


# ---------- Tests ----------


@pytest.mark.asyncio
async def test_subagent_emits_report_fires_result_event(tmp_path: Path):
    """Happy path: subagent's first iteration emits Report; runner converts
    it into a ToolResultArrived Perception."""
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
    runner, queue = _make_runner(adapter, tmp_path)

    runner.spawn(
        sub_id="abc12345",
        task="search transcripts for coffee",
        timeout_s=30,
    )

    p = await _wait_for_tool_result(queue)
    assert p.kind == "ToolResultArrived"
    assert p.data["tool"] == "Subagent"
    assert p.data["task_id"] == "abc12345"
    assert p.data["task"] == "search transcripts for coffee"
    assert p.data["status"] == "ok"
    assert p.data["summary"] == "found 3 files"
    assert "a.md" in p.data["details"]


@pytest.mark.asyncio
async def test_subagent_timeout_fires_event_with_timeout_status(tmp_path: Path):
    adapter = _ScriptedAdapter(
        scripts=[[StreamChunk(text="", done=True)]],
        delay_per_iter=10.0,  # adapter never returns within timeout
    )
    runner, queue = _make_runner(adapter, tmp_path)

    runner.spawn(
        sub_id="t1",
        task="something slow",
        timeout_s=1,
    )

    p = await _wait_for_tool_result(queue, timeout=3.0)
    assert p.data["status"] == "timeout"
    assert "timeout" in p.data["summary"].lower()


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
    runner, queue = _make_runner(adapter, tmp_path)

    runner.spawn(
        sub_id="nr1",
        task="do nothing",
        timeout_s=10,
    )

    p = await _wait_for_tool_result(queue)
    assert p.data["status"] == "no_report"


@pytest.mark.asyncio
async def test_subagent_runtime_error_fires_error_status(tmp_path: Path):
    """If the adapter raises, runner catches and dispatches status='error'."""

    class _BoomAdapter(LLMAdapter):
        async def stream_completion(self, **_):  # pragma: no cover
            yield StreamChunk(text="", done=True)

        async def stream_messages(self, **_):
            raise RuntimeError("kaboom")
            yield  # pragma: no cover — make this an async generator

    runner, queue = _make_runner(_BoomAdapter(), tmp_path)

    runner.spawn(
        sub_id="err1",
        task="anything",
        timeout_s=10,
    )

    p = await _wait_for_tool_result(queue)
    assert p.data["status"] == "error"
    assert "kaboom" in p.data["details"]
    assert "RuntimeError" in p.data["summary"]


@pytest.mark.asyncio
async def test_multiple_subagents_run_concurrently(tmp_path: Path):
    """Spawn 3 — all complete and 3 perceptions fire."""
    adapter = _ScriptedAdapter(
        scripts=[
            [StreamChunk(text=_report_call("ok", f"s{i}", "d"), done=False),
             StreamChunk(text="", done=True)]
            for i in range(3)
        ]
    )
    runner, queue = _make_runner(adapter, tmp_path)

    for i in range(3):
        runner.spawn(
            sub_id=f"id{i}",
            task=f"task {i}",
            timeout_s=10,
        )

    perceptions: list[Perception] = []
    async with asyncio.timeout(2.0):
        while len(perceptions) < 3:
            batch = await queue.drain(timeout_s=0.05)
            perceptions.extend(p for p in batch if p.kind == "ToolResultArrived")
    assert len(perceptions) == 3
    statuses = {p.data["status"] for p in perceptions}
    assert statuses == {"ok"}
    ids = {p.data["task_id"] for p in perceptions}
    assert ids == {"id0", "id1", "id2"}


@pytest.mark.asyncio
async def test_subagent_stuck_tool_aborts_with_no_report(tmp_path: Path):
    """3 consecutive same-tool failures abort cascade → no_report event."""
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
    runner, queue = _make_runner(adapter, tmp_path)

    runner.spawn(
        sub_id="stuck1",
        task="stuck",
        timeout_s=10,
    )

    p = await _wait_for_tool_result(queue, timeout=2.0)
    assert p.data["status"] == "no_report"


@pytest.mark.asyncio
async def test_subagent_writes_full_details_to_store(tmp_path: Path) -> None:
    """Happy path: subagent with long details writes all lines to store,
    perception carries preview + output_id + line_count."""
    long_details = "\n".join(f"finding {i}" for i in range(50))
    adapter = _ScriptedAdapter(
        scripts=[
            [
                StreamChunk(
                    text=_report_call("ok", "50 findings", long_details),
                    done=False,
                ),
                StreamChunk(text="", done=True),
            ],
        ]
    )
    store = ToolOutputStore(tmp_path / "tool_outputs")
    queue = PerceptionQueue()
    runner = SubagentRunner(
        adapter=adapter,
        renderer=PromptRenderer(),
        memory_root=tmp_path / "memory",
        memsearch=_FakeMemSearch(),
        transcripts_root=tmp_path / "transcripts",
        tool_output_store=store,
        perception_queue=queue,
    )

    runner.spawn(
        sub_id="paging1",
        task="find findings",
        timeout_s=30,
    )

    p = await _wait_for_tool_result(queue)
    assert p.data["details_output_id"] is not None
    assert p.data["details_line_count"] == 50
    assert len(p.data["details"].splitlines()) <= 20  # preview only
    # Full content is in the store
    full = store.read(p.data["details_output_id"], offset=0, limit=100)
    assert full.lines[0] == "finding 0"
    assert full.lines[49] == "finding 49"


def test_sub_tools_excludes_spawn_subagent():
    """Subagent toolkit explicitly omits SpawnSubagent; includes Report."""
    assert SpawnSubagent not in SUB_TOOLS
    assert Report in SUB_TOOLS


def test_sub_grammar_excludes_spawn_subagent():
    """Grammar built from SUB_TOOLS does not list SpawnSubagent in
    its tool-name enum or per-tool call rules."""
    g = build_qwen3_think_tool_grammar(SUB_TOOLS)
    assert '"SpawnSubagent"' not in g
    assert "spawn-subagent-call" not in g
    # but Report IS there
    assert '"Report"' in g
    assert "report-call ::= " in g


@pytest.mark.asyncio
async def test_subagent_dispatches_only_after_completion(tmp_path: Path):
    """ToolResultArrived is only enqueued when the cascade returns
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

    runner, queue = _make_runner(_SlowAdapter(), tmp_path)
    runner.spawn(sub_id="s1", task="x", timeout_s=5)

    # Right after spawn — no ToolResultArrived yet.
    batch = await queue.drain(timeout_s=0.01)
    assert not any(p.kind == "ToolResultArrived" for p in batch)

    await asyncio.wait_for(completed.wait(), timeout=1.0)
    # Give the runner a tick to enqueue.
    await asyncio.sleep(0.05)
    p = await _wait_for_tool_result(queue, timeout=1.0)
    assert p.kind == "ToolResultArrived"


@pytest.mark.asyncio
async def test_subagent_runner_stop_cancels_inflight(tmp_path: Path):
    """stop() cancels in-flight tasks; no perception is enqueued for them."""

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
    runner, queue = _make_runner(adapter, tmp_path)
    runner.spawn(sub_id="hang1", task="x", timeout_s=60)
    await asyncio.wait_for(adapter.entered.wait(), timeout=1.0)
    await asyncio.wait_for(runner.stop(), timeout=1.0)
    # No ToolResultArrived dispatched (cancellation skips the dispatch path).
    tool_results = []
    for _ in range(5):
        batch = await queue.drain(timeout_s=0.05)
        tool_results.extend(p for p in batch if p.kind == "ToolResultArrived")
    assert tool_results == []


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
    runner, queue = _make_runner(adapter, tmp_path)
    runner.spawn(sub_id="s1", task="hello", timeout_s=10)
    await _wait_for_tool_result(queue)

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
    MindCtx so subagents can use Shell."""
    from dollos.mind.mind_ctx import MindCtx
    from dollos.tools import SUB_TOOLS

    captured: list[MindCtx] = []

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
        queue = PerceptionQueue()
        shell_runner = ShellRunner(
            cwd=tmp_path,
            perception_queue=queue,
            tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        )
        runner = SubagentRunner(
            adapter=adapter,
            renderer=PromptRenderer(),
            memory_root=tmp_path / "memory",
            memsearch=_FakeMemSearch(),
            transcripts_root=tmp_path / "transcripts",
            shell_runner=shell_runner,
            tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
            perception_queue=queue,
        )
        runner.spawn(
            sub_id="reg1",
            task="capture ctx",
            timeout_s=10,
        )
        await _wait_for_tool_result(queue, timeout=3.0)

        assert captured, "tool was not invoked inside sub-cascade"
        assert captured[0].shell_runner is shell_runner
    finally:
        SUB_TOOLS.clear()
        SUB_TOOLS.extend(SUB_TOOLS_orig)


# ----- MonitorRunner on sub-cascade ctx -----


@pytest.mark.asyncio
async def test_subagent_ctx_has_monitor_runner(tmp_path: Path):
    """SubagentRunner forwards its monitor_runner into the sub-cascade
    MindCtx so subagents can use SpawnMonitor/RemoveMonitor."""
    from dollos.mind.mind_ctx import MindCtx
    from dollos.tools import SUB_TOOLS

    captured: list[MindCtx] = []

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
        queue = PerceptionQueue()
        monitor_runner = MonitorRunner(cwd=tmp_path, perception_queue=queue)
        runner = SubagentRunner(
            adapter=adapter,
            renderer=PromptRenderer(),
            memory_root=tmp_path / "memory",
            memsearch=_FakeMemSearch(),
            transcripts_root=tmp_path / "transcripts",
            monitor_runner=monitor_runner,
            tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
            perception_queue=queue,
        )
        runner.spawn(
            sub_id="reg2",
            task="capture ctx",
            timeout_s=10,
        )
        await _wait_for_tool_result(queue, timeout=3.0)

        assert captured, "tool was not invoked inside sub-cascade"
        assert captured[0].monitor_runner is monitor_runner
    finally:
        SUB_TOOLS.clear()
        SUB_TOOLS.extend(SUB_TOOLS_orig)
