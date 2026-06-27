"""Tests for WorkflowRunner — deterministic fan-out of worker agents that
re-enters ONE ToolResultArrived(tool="Workflow") perception with the combined
result. Migrated from the retired test_subagent.py (helpers + N=1 paths) plus
the new fan-out / synthesis / verify / concurrency behaviors."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel

import dollos.workflow as workflow_mod
from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.mind.mind_state import Perception
from dollos.mind.perception_queue import PerceptionQueue
from dollos.monitor_runner import MonitorRunner
from dollos.prompts import PromptRenderer
from dollos.shell_runner import ShellRunner
from dollos.tool_outputs import ToolOutputStore
from dollos.workflow import MAX_WORKFLOW_CONCURRENCY, WorkflowRunner


# ---------- Fakes ----------


def _report_call(status: str, summary: str, details: str) -> str:
    """Render a Report tool_call XML block (single-chunk emit)."""
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


@dataclass
class _ScriptedAdapter(LLMAdapter):
    """Fake LLMAdapter that pops a pre-scripted chunk list per call."""

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
        chunks = [StreamChunk(text="", done=True)] if not self.scripts else self.scripts.pop(0)
        if self.delay_per_iter:
            await asyncio.sleep(self.delay_per_iter)
        for c in chunks:
            yield c


@dataclass
class _WorkerAdapter(LLMAdapter):
    """Task-aware fake — inspects messages[0]['content'] to shape its reply.

    - content contains 'BOOM' → raise RuntimeError (error degraded path)
    - content contains 'SLOW' → sleep `slow_hold` then Report (per-agent timeout)
    - else → immediate Report(ok, summary=content[:60], details="details::...")

    Tracks adapter concurrency (in_flight / peak) for the semaphore-cap test.
    """

    slow_hold: float = 1.0
    hold: float = 0.0
    calls: list[dict] = field(default_factory=list)
    in_flight: int = 0
    peak: int = 0

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
        content = messages[0]["content"] if messages else ""
        self.calls.append({"system": system, "content": content})
        if "BOOM" in content:
            raise RuntimeError("kaboom")
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if "SLOW" in content:
                await asyncio.sleep(self.slow_hold)
            elif self.hold:
                await asyncio.sleep(self.hold)
        finally:
            self.in_flight -= 1
        yield StreamChunk(
            text=_report_call("ok", content[:60], f"details::{content[:60]}"),
            done=False,
        )
        yield StreamChunk(text="", done=True)


class _FakeMemSearch:
    def __init__(self) -> None:
        self.indexed: list = []

    async def index_file(self, path):
        self.indexed.append(path)

    async def search(self, query: str, top_k: int = 5):
        return []


def _make_runner(
    adapter: LLMAdapter,
    tmp_path: Path,
    queue: PerceptionQueue | None = None,
    *,
    shell_runner: ShellRunner | None = None,
    monitor_runner: MonitorRunner | None = None,
) -> tuple[WorkflowRunner, PerceptionQueue, ToolOutputStore]:
    q = queue if queue is not None else PerceptionQueue()
    store = ToolOutputStore(tmp_path / "tool_outputs")
    runner = WorkflowRunner(
        adapter=adapter,
        renderer=PromptRenderer(),
        memory_root=tmp_path / "memory",
        memsearch=_FakeMemSearch(),
        transcripts_root=tmp_path / "transcripts",
        tool_output_store=store,
        perception_queue=q,
        shell_runner=shell_runner,
        monitor_runner=monitor_runner,
    )
    return runner, q, store


async def _wait_for_tool_result(queue: PerceptionQueue, timeout: float = 2.0) -> Perception:
    """Poll until the first ToolResultArrived perception arrives."""
    async with asyncio.timeout(timeout):
        while True:
            perceptions = await queue.drain(timeout_s=0.01)
            for p in perceptions:
                if p.kind == "ToolResultArrived":
                    return p


async def _collect_tool_results(
    queue: PerceptionQueue, settle: float = 0.3
) -> list[Perception]:
    """Drain ToolResultArrived perceptions over a settle window (count check)."""
    found: list[Perception] = []
    deadline = asyncio.get_event_loop().time() + settle
    while asyncio.get_event_loop().time() < deadline:
        for p in await queue.drain(timeout_s=0.02):
            if p.kind == "ToolResultArrived":
                found.append(p)
    return found


# ---------- N=1 degenerate paths (migrated from test_subagent) ----------


@pytest.mark.asyncio
async def test_n1_report_fires_result_event(tmp_path: Path):
    """N=1, no synthesis: the single worker's Report becomes the result."""
    adapter = _ScriptedAdapter(
        scripts=[[
            StreamChunk(text=_report_call("ok", "found 3 files", "a.md, b.md, c.md"), done=False),
            StreamChunk(text="", done=True),
        ]]
    )
    runner, queue, _ = _make_runner(adapter, tmp_path)
    runner.spawn(
        workflow_id="wf-abc123",
        tasks=["search transcripts for coffee"],
        synthesis=None,
        mode="map_reduce",
        timeout_s=30,
    )

    p = await _wait_for_tool_result(queue)
    assert p.data["tool"] == "Workflow"
    assert p.data["task_id"] == "wf-abc123"
    assert p.data["task"] == "workflow: 1 tasks, mode=map_reduce"
    assert p.data["status"] == "ok"
    assert p.data["summary"] == "found 3 files"
    assert "a.md" in p.data["details"]


@pytest.mark.asyncio
async def test_n1_whole_workflow_timeout(tmp_path: Path):
    adapter = _ScriptedAdapter(
        scripts=[[StreamChunk(text="", done=True)]],
        delay_per_iter=10.0,
    )
    runner, queue, _ = _make_runner(adapter, tmp_path)
    runner.spawn(
        workflow_id="wf-t1",
        tasks=["something slow"],
        synthesis=None,
        mode="map_reduce",
        timeout_s=1,
    )

    p = await _wait_for_tool_result(queue, timeout=3.0)
    assert p.data["status"] == "timeout"
    assert "timeout" in p.data["summary"].lower()


@pytest.mark.asyncio
async def test_n1_no_report(tmp_path: Path):
    adapter = _ScriptedAdapter(
        scripts=[[StreamChunk(text="(idle thinking)", done=False), StreamChunk(text="", done=True)]]
    )
    runner, queue, _ = _make_runner(adapter, tmp_path)
    runner.spawn(
        workflow_id="wf-nr1", tasks=["do nothing"], synthesis=None,
        mode="map_reduce", timeout_s=10,
    )

    p = await _wait_for_tool_result(queue)
    assert p.data["status"] == "no_report"


@pytest.mark.asyncio
async def test_n1_runtime_error(tmp_path: Path):
    """A crashing agent degrades to a status='error' report (raw for N=1)."""

    class _BoomAdapter(LLMAdapter):
        async def stream_completion(self, **_):  # pragma: no cover
            yield StreamChunk(text="", done=True)

        async def stream_messages(self, **_):
            raise RuntimeError("kaboom")
            yield  # pragma: no cover

    runner, queue, _ = _make_runner(_BoomAdapter(), tmp_path)
    runner.spawn(
        workflow_id="wf-err1", tasks=["anything"], synthesis=None,
        mode="map_reduce", timeout_s=10,
    )

    p = await _wait_for_tool_result(queue)
    assert p.data["status"] == "error"
    assert "kaboom" in p.data["details"]
    assert "RuntimeError" in p.data["summary"]


@pytest.mark.asyncio
async def test_n1_stuck_tool_aborts_with_no_report(tmp_path: Path):
    """3 consecutive same-tool failures abort the cascade → no_report."""
    bad_call = '<tool_call>{"name":"Shell","arguments":{}}</tool_call>'
    adapter = _ScriptedAdapter(
        scripts=[
            [StreamChunk(text=bad_call, done=False), StreamChunk(text="", done=True)]
            for _ in range(3)
        ]
    )
    runner, queue, _ = _make_runner(adapter, tmp_path)
    runner.spawn(
        workflow_id="wf-stuck1", tasks=["stuck"], synthesis=None,
        mode="map_reduce", timeout_s=10,
    )

    p = await _wait_for_tool_result(queue, timeout=2.0)
    assert p.data["status"] == "no_report"


@pytest.mark.asyncio
async def test_n1_details_paging(tmp_path: Path):
    """Long details: result carries preview + output_id; full text in store."""
    long_details = "\n".join(f"finding {i}" for i in range(50))
    adapter = _ScriptedAdapter(
        scripts=[[
            StreamChunk(text=_report_call("ok", "50 findings", long_details), done=False),
            StreamChunk(text="", done=True),
        ]]
    )
    runner, queue, store = _make_runner(adapter, tmp_path)
    runner.spawn(
        workflow_id="wf-paging1", tasks=["find findings"], synthesis=None,
        mode="map_reduce", timeout_s=30,
    )

    p = await _wait_for_tool_result(queue)
    assert p.data["details_output_id"] is not None
    assert p.data["details_line_count"] == 50
    assert len(p.data["details"].splitlines()) <= workflow_mod.WORKFLOW_PREVIEW_LINES
    full = store.read(p.data["details_output_id"], offset=0, limit=100)
    assert full.lines[0] == "finding 0"
    assert full.lines[49] == "finding 49"


@pytest.mark.asyncio
async def test_stop_cancels_inflight(tmp_path: Path):
    """stop() cancels in-flight workflows; no perception is enqueued."""

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
    runner, queue, _ = _make_runner(adapter, tmp_path)
    runner.spawn(
        workflow_id="wf-hang1", tasks=["x"], synthesis=None,
        mode="map_reduce", timeout_s=60,
    )
    await asyncio.wait_for(adapter.entered.wait(), timeout=1.0)
    await asyncio.wait_for(runner.stop(), timeout=1.0)
    assert await _collect_tool_results(queue, settle=0.2) == []


@pytest.mark.asyncio
async def test_worker_uses_subagent_scaffolding(tmp_path: Path):
    """Worker agent's system prompt is the subagent scaffolding (no Identity)."""
    adapter = _ScriptedAdapter(
        scripts=[[StreamChunk(text=_report_call("ok", "x", "y"), done=False),
                  StreamChunk(text="", done=True)]]
    )
    runner, queue, _ = _make_runner(adapter, tmp_path)
    runner.spawn(
        workflow_id="wf-s1", tasks=["hello"], synthesis=None,
        mode="map_reduce", timeout_s=10,
    )
    await _wait_for_tool_result(queue)

    assert len(adapter.calls) == 1
    system = adapter.calls[0]["system"]
    assert "worker agent" in system   # subagent_scaffolding template (renamed from "subagent")
    assert "Report" in system
    assert "# Identity" not in system
    assert "[Memory context]" not in system
    assert adapter.calls[0]["messages"][0] == {"role": "user", "content": "hello"}


# ---------- Fan-out / concurrency / synthesis / verify ----------


@pytest.mark.asyncio
async def test_n_tasks_concurrent_all_collected(tmp_path: Path):
    """(a) N tasks run; no-synthesis roll-up collects every report."""
    adapter = _WorkerAdapter(hold=0.02)
    runner, queue, store = _make_runner(adapter, tmp_path)
    tasks = ["t-alpha", "t-beta", "t-gamma"]
    runner.spawn(
        workflow_id="wf-fan", tasks=tasks, synthesis=None,
        mode="map_reduce", timeout_s=30,
    )

    p = await _wait_for_tool_result(queue)
    assert p.data["status"] == "ok"
    assert p.data["summary"] == "3 tasks: 3 ok, 0 not ok"
    full = store.read(p.data["details_output_id"], offset=0, limit=500)
    text = "\n".join(full.lines)
    for t in tasks:
        assert t in text


@pytest.mark.asyncio
async def test_semaphore_caps_in_flight(tmp_path: Path):
    """(b) 12 tasks, Semaphore(8): peak concurrent adapter calls never > 8."""
    adapter = _WorkerAdapter(hold=0.1)
    runner, queue, _ = _make_runner(adapter, tmp_path)
    runner.spawn(
        workflow_id="wf-sem",
        tasks=[f"task-{i}" for i in range(12)],
        synthesis=None,
        mode="map_reduce",
        timeout_s=30,
    )

    p = await _wait_for_tool_result(queue, timeout=5.0)
    assert p.data["summary"] == "12 tasks: 12 ok, 0 not ok"
    assert 2 <= adapter.peak <= MAX_WORKFLOW_CONCURRENCY


@pytest.mark.asyncio
async def test_synthesis_combines(tmp_path: Path):
    """(c) Synthesis task text contains each worker summary; exactly one result."""
    adapter = _WorkerAdapter()
    runner, queue, _ = _make_runner(adapter, tmp_path)
    runner.spawn(
        workflow_id="wf-syn",
        tasks=["task-AAA", "task-BBB"],
        synthesis="MERGE_THEM",
        mode="map_reduce",
        timeout_s=30,
    )

    p = await _wait_for_tool_result(queue)
    assert p.data["tool"] == "Workflow"
    # Exactly one ToolResultArrived perception for the whole workflow.
    extra = await _collect_tool_results(queue, settle=0.2)
    assert extra == []

    synth_calls = [c for c in adapter.calls if "MERGE_THEM" in c["content"]]
    assert len(synth_calls) == 1
    synth_text = synth_calls[0]["content"]
    assert "task-AAA" in synth_text
    assert "task-BBB" in synth_text


@pytest.mark.asyncio
async def test_verify_mode_invocation_count(tmp_path: Path):
    """(d) mode=verify with synthesis → N task + N verify + 1 synthesis agents."""
    adapter = _WorkerAdapter()
    runner, queue, _ = _make_runner(adapter, tmp_path)
    runner.spawn(
        workflow_id="wf-verify",
        tasks=["v-1", "v-2"],
        synthesis="DONE",
        mode="verify",
        timeout_s=30,
    )

    await _wait_for_tool_result(queue)
    # 2 task agents + 2 verify skeptics + 1 synthesis agent.
    assert len(adapter.calls) == 2 + 2 + 1
    assert any("adversarial skeptic" in c["content"] for c in adapter.calls)


@pytest.mark.asyncio
async def test_n1_verify_skeptic_verdict_surfaces(tmp_path: Path):
    """M1 regression: N=1, mode=verify, synthesis=None → skeptic verdict folds into perception.

    Without the fix, the raw early-return discards the verify key; the perception
    is identical to a bare map_reduce N=1 result (no [verify] block in details).
    With the fix, _rollup is called so the skeptic's verdict is always visible.
    """
    worker_report = _report_call("ok", "task done", "task output")
    skeptic_report = _report_call("incomplete", "refutation found", "evidence against claim")
    adapter = _ScriptedAdapter(
        scripts=[
            [StreamChunk(text=worker_report, done=False), StreamChunk(text="", done=True)],
            [StreamChunk(text=skeptic_report, done=False), StreamChunk(text="", done=True)],
        ]
    )
    runner, queue, _ = _make_runner(adapter, tmp_path)
    runner.spawn(
        workflow_id="wf-verify-n1",
        tasks=["check something"],
        synthesis=None,
        mode="verify",
        timeout_s=30,
    )

    p = await _wait_for_tool_result(queue)
    assert p.data["tool"] == "Workflow"
    details = p.data["details"]
    # The rollup MUST include the skeptic verdict — it must not be the raw worker details
    assert "[verify]" in details, "skeptic verdict missing from details"
    assert "incomplete" in details, "skeptic status (incomplete) missing from details"
    assert "refutation found" in details, "skeptic summary missing from details"
    # Confirm the worker content is also present (rollup, not replacement)
    assert "task done" in details or "task output" in details


@pytest.mark.asyncio
async def test_n1_no_synthesis_raw(tmp_path: Path):
    """(e) N=1, no synthesis → that worker's report returned raw."""
    adapter = _WorkerAdapter()
    runner, queue, _ = _make_runner(adapter, tmp_path)
    runner.spawn(
        workflow_id="wf-raw", tasks=["only-task"], synthesis=None,
        mode="map_reduce", timeout_s=30,
    )

    p = await _wait_for_tool_result(queue)
    assert p.data["status"] == "ok"
    assert p.data["summary"] == "only-task"  # echoed raw, no roll-up wrapping


@pytest.mark.asyncio
async def test_partial_failure_rollup_with_synthesis(tmp_path: Path):
    """(f) A degraded task feeds synthesis; any task error → workflow incomplete."""
    adapter = _WorkerAdapter()
    runner, queue, _ = _make_runner(adapter, tmp_path)
    runner.spawn(
        workflow_id="wf-partial",
        tasks=["ok-1", "BOOM-2", "ok-3"],
        synthesis="SYNTH",
        mode="map_reduce",
        timeout_s=30,
    )

    p = await _wait_for_tool_result(queue)
    # Synthesis ran (returns ok) but a task errored → workflow downgraded.
    assert p.data["status"] == "incomplete"
    synth_calls = [c for c in adapter.calls if "SYNTH" in c["content"]]
    assert len(synth_calls) == 1
    synth_text = synth_calls[0]["content"]
    assert "ok-1" in synth_text
    assert "status=error" in synth_text


@pytest.mark.asyncio
async def test_no_nesting_worker_ctx_workflow_runner_is_none(tmp_path: Path):
    """(g) Worker ctx.workflow_runner is None (no-nesting guard)."""
    from dollos.mind.mind_ctx import MindCtx
    from dollos.tools import SUB_TOOLS

    captured: list[MindCtx] = []

    class _CaptureTool(BaseModel):
        token: str = "x"

        async def run(self, ctx) -> None:
            captured.append(ctx)
            return None

    orig = list(SUB_TOOLS)
    SUB_TOOLS.clear()
    SUB_TOOLS.extend([_CaptureTool])
    try:
        adapter = _ScriptedAdapter(
            scripts=[[
                StreamChunk(
                    text='<tool_call>{"name":"_CaptureTool","arguments":{"token":"x"}}</tool_call>',
                    done=False,
                ),
                StreamChunk(text="", done=True),
            ]]
        )
        runner, queue, _ = _make_runner(adapter, tmp_path)
        runner.spawn(
            workflow_id="wf-nest", tasks=["capture ctx"], synthesis=None,
            mode="map_reduce", timeout_s=10,
        )
        await _wait_for_tool_result(queue, timeout=3.0)
        assert captured, "tool was not invoked inside worker cascade"
        assert captured[0].workflow_runner is None
    finally:
        SUB_TOOLS.clear()
        SUB_TOOLS.extend(orig)


@pytest.mark.asyncio
async def test_per_agent_timeout_degrades_one_task(tmp_path: Path, monkeypatch):
    """(h) Per-agent timeout degrades the slow task only; others complete."""
    monkeypatch.setattr(workflow_mod, "MAX_AGENT_TIMEOUT_S", 0.2)
    adapter = _WorkerAdapter(slow_hold=1.0)
    runner, queue, store = _make_runner(adapter, tmp_path)
    runner.spawn(
        workflow_id="wf-pat",
        tasks=["fast-1", "fast-2", "SLOW-3"],
        synthesis=None,
        mode="map_reduce",
        timeout_s=30,
    )

    p = await _wait_for_tool_result(queue, timeout=3.0)
    assert p.data["status"] == "incomplete"
    assert p.data["summary"] == "3 tasks: 2 ok, 1 not ok"
    full = store.read(p.data["details_output_id"], offset=0, limit=500)
    text = "\n".join(full.lines)
    # Exactly one task degraded to timeout; the other two completed ok.
    assert text.count("] timeout |") == 1
    assert text.count("] ok |") == 2


@pytest.mark.asyncio
async def test_exactly_one_perception_per_workflow(tmp_path: Path):
    """(i) A multi-task workflow emits exactly one ToolResultArrived."""
    adapter = _WorkerAdapter(hold=0.01)
    runner, queue, _ = _make_runner(adapter, tmp_path)
    runner.spawn(
        workflow_id="wf-one", tasks=["a", "b", "c"], synthesis=None,
        mode="map_reduce", timeout_s=30,
    )
    await _wait_for_tool_result(queue)
    extra = await _collect_tool_results(queue, settle=0.3)
    assert extra == []


@pytest.mark.asyncio
async def test_multiple_workflows_distinct_perceptions(tmp_path: Path):
    """Concurrent workflows each fire their own distinct result perception."""
    adapter = _WorkerAdapter(hold=0.02)
    runner, queue, _ = _make_runner(adapter, tmp_path)
    for i in range(3):
        runner.spawn(
            workflow_id=f"wf-{i}", tasks=[f"task-{i}"], synthesis=None,
            mode="map_reduce", timeout_s=10,
        )

    perceptions: list[Perception] = []
    async with asyncio.timeout(3.0):
        while len(perceptions) < 3:
            for p in await queue.drain(timeout_s=0.05):
                if p.kind == "ToolResultArrived":
                    perceptions.append(p)
    assert {p.data["task_id"] for p in perceptions} == {"wf-0", "wf-1", "wf-2"}
    assert {p.data["status"] for p in perceptions} == {"ok"}


# ---------- Forwarded ctx deps ----------


@pytest.mark.asyncio
async def test_worker_ctx_has_shell_runner(tmp_path: Path):
    """WorkflowRunner forwards shell_runner into the worker MindCtx."""
    from dollos.mind.mind_ctx import MindCtx
    from dollos.tools import SUB_TOOLS

    captured: list[MindCtx] = []

    class _CaptureTool(BaseModel):
        token: str = "x"

        async def run(self, ctx) -> None:
            captured.append(ctx)
            return None

    orig = list(SUB_TOOLS)
    SUB_TOOLS.clear()
    SUB_TOOLS.extend([_CaptureTool])
    try:
        adapter = _ScriptedAdapter(
            scripts=[[
                StreamChunk(
                    text='<tool_call>{"name":"_CaptureTool","arguments":{"token":"x"}}</tool_call>',
                    done=False,
                ),
                StreamChunk(text="", done=True),
            ]]
        )
        queue = PerceptionQueue()
        shell_runner = ShellRunner(
            cwd=tmp_path,
            perception_queue=queue,
            tool_output_store=ToolOutputStore(tmp_path / "shell_outputs"),
        )
        runner, _, _ = _make_runner(adapter, tmp_path, queue, shell_runner=shell_runner)
        runner.spawn(
            workflow_id="wf-shell", tasks=["capture ctx"], synthesis=None,
            mode="map_reduce", timeout_s=10,
        )
        await _wait_for_tool_result(queue, timeout=3.0)
        assert captured
        assert captured[0].shell_runner is shell_runner
    finally:
        SUB_TOOLS.clear()
        SUB_TOOLS.extend(orig)


@pytest.mark.asyncio
async def test_worker_ctx_has_monitor_runner(tmp_path: Path):
    """WorkflowRunner forwards monitor_runner into the worker MindCtx."""
    from dollos.mind.mind_ctx import MindCtx
    from dollos.tools import SUB_TOOLS

    captured: list[MindCtx] = []

    class _CaptureTool(BaseModel):
        token: str = "x"

        async def run(self, ctx) -> None:
            captured.append(ctx)
            return None

    orig = list(SUB_TOOLS)
    SUB_TOOLS.clear()
    SUB_TOOLS.extend([_CaptureTool])
    try:
        adapter = _ScriptedAdapter(
            scripts=[[
                StreamChunk(
                    text='<tool_call>{"name":"_CaptureTool","arguments":{"token":"x"}}</tool_call>',
                    done=False,
                ),
                StreamChunk(text="", done=True),
            ]]
        )
        queue = PerceptionQueue()
        monitor_runner = MonitorRunner(cwd=tmp_path, perception_queue=queue)
        runner, _, _ = _make_runner(adapter, tmp_path, queue, monitor_runner=monitor_runner)
        runner.spawn(
            workflow_id="wf-mon", tasks=["capture ctx"], synthesis=None,
            mode="map_reduce", timeout_s=10,
        )
        await _wait_for_tool_result(queue, timeout=3.0)
        assert captured
        assert captured[0].monitor_runner is monitor_runner
    finally:
        SUB_TOOLS.clear()
        SUB_TOOLS.extend(orig)


# ---------- _rollup_status regression ----------


def test_rollup_status_all_ok_synthesis_degraded_gives_incomplete():
    """Regression: if all task reports are ok but the synthesis agent itself
    timed out or crashed (status not in {ok, incomplete}), the whole workflow
    status must be 'incomplete', not the raw degraded string from the runner."""
    from dollos.workflow import WorkflowRunner

    task_reports = [
        {"status": "ok", "summary": "task 0 done", "details": ""},
        {"status": "ok", "summary": "task 1 done", "details": ""},
    ]

    # Synthesis agent timed out
    synth_timeout = {"status": "timeout", "summary": "synthesis timed out", "details": ""}
    assert WorkflowRunner._rollup_status(task_reports, synth_timeout) == "incomplete"

    # Synthesis agent crashed
    synth_error = {"status": "error", "summary": "synthesis crashed", "details": ""}
    assert WorkflowRunner._rollup_status(task_reports, synth_error) == "incomplete"

    # Synthesis agent produced no report
    synth_noreport = {"status": "no_report", "summary": "no report", "details": ""}
    assert WorkflowRunner._rollup_status(task_reports, synth_noreport) == "incomplete"

    # Sanity: all ok + synthesis ok → ok
    synth_ok = {"status": "ok", "summary": "combined", "details": ""}
    assert WorkflowRunner._rollup_status(task_reports, synth_ok) == "ok"

    # Sanity: all ok + synthesis incomplete → incomplete
    synth_incomplete = {"status": "incomplete", "summary": "partial", "details": ""}
    assert WorkflowRunner._rollup_status(task_reports, synth_incomplete) == "incomplete"


# ---------- Timing / dispatch ----------


@pytest.mark.asyncio
async def test_eager_dispatch_queue_empty_after_spawn(tmp_path: Path):
    """spawn() returns immediately (fire-and-forget); the queue is still empty
    right after the call — the workflow runs entirely in the background."""

    class _HangAdapter(LLMAdapter):
        async def stream_completion(self, **_):  # pragma: no cover
            yield StreamChunk(text="", done=True)

        async def stream_messages(self, **_):
            await asyncio.Event().wait()  # hang until cancelled
            yield StreamChunk(text="", done=True)  # pragma: no cover

    runner, queue, _ = _make_runner(_HangAdapter(), tmp_path)
    runner.spawn(
        workflow_id="wf-eager", tasks=["slow-task"], synthesis=None,
        mode="map_reduce", timeout_s=60,
    )
    # No await — check synchronously that nothing arrived yet.
    # drain(timeout_s=0.0) fires after one event-loop tick; the workflow
    # is still blocked on the hanging adapter at that point.
    drained = await queue.drain(timeout_s=0.0)
    assert [p for p in drained if p.kind == "ToolResultArrived"] == [], (
        "spawn() must be fire-and-forget; result must not appear before workflow completes"
    )
    await runner.stop()
