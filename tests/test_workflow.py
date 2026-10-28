"""Tests for WorkflowRunner — deterministic fan-out of worker agents that
re-enters ONE ToolResultArrived(tool="Workflow") perception with the combined
result. Migrated from the retired test_subagent.py (helpers + N=1 paths) plus
the new fan-out / synthesis / verify / concurrency behaviors."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

import dollos.workflow as workflow_mod
from dollos.llm.model import ModelAdapter
from dollos.mind.mind_state import Perception
from dollos.mind.perception_queue import PerceptionQueue
from dollos.monitor_runner import MonitorRunner
from dollos.prompts import PromptRenderer
from dollos.shell_runner import ShellRunner
from dollos.tool_outputs import ToolOutputStore
from dollos.workflow import MAX_WORKFLOW_CONCURRENCY, WorkflowRunner
from tests._dispatcher_helpers import _HangModel
from tests._fake_model import FakeModel


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


def _task_of(call: dict) -> str:
    """The agent's task: the user message right after the system prompt."""
    return call["messages"][1]["content"]


class _WorkerModel(FakeModel):
    """Task-aware fake — inspects the task message to shape its reply.

    - task contains 'BOOM' → raise RuntimeError (error degraded path)
    - task contains 'SLOW' → sleep `slow_hold` then Report (per-agent timeout)
    - else → immediate Report(ok, summary=task[:60], details="details::...")

    Tracks model concurrency (in_flight / peak) for the semaphore-cap test.
    """

    def __init__(self, *, slow_hold: float = 1.0, hold: float = 0.0) -> None:
        super().__init__()
        self.slow_hold = slow_hold
        self.hold = hold
        self.in_flight = 0
        self.peak = 0

    def _reply(self, idx, messages):
        task = messages[1]["content"]
        return _report_call("ok", task[:60], f"details::{task[:60]}")

    async def stream(self, **kw):
        task = kw["messages"][1]["content"]
        if "BOOM" in task:
            raise RuntimeError("kaboom")
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if "SLOW" in task:
                await asyncio.sleep(self.slow_hold)
            elif self.hold:
                await asyncio.sleep(self.hold)
        finally:
            self.in_flight -= 1
        async for ev in super().stream(**kw):
            yield ev


class _FakeMemSearch:
    def __init__(self) -> None:
        self.indexed: list = []

    async def index_file(self, path):
        self.indexed.append(path)

    async def search(self, query: str, top_k: int = 5):
        return []


def _make_runner(
    model: ModelAdapter,
    tmp_path: Path,
    queue: PerceptionQueue | None = None,
    *,
    shell_runner: ShellRunner | None = None,
    monitor_runner: MonitorRunner | None = None,
) -> tuple[WorkflowRunner, PerceptionQueue, ToolOutputStore]:
    q = queue if queue is not None else PerceptionQueue()
    store = ToolOutputStore(tmp_path / "tool_outputs")
    runner = WorkflowRunner(
        model=model,
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
    model = FakeModel([_report_call("ok", "found 3 files", "a.md, b.md, c.md")])
    runner, queue, _ = _make_runner(model, tmp_path)
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
    model = FakeModel(delay_s=10.0)
    runner, queue, _ = _make_runner(model, tmp_path)
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
    model = FakeModel(["(idle thinking)"])
    runner, queue, _ = _make_runner(model, tmp_path)
    runner.spawn(
        workflow_id="wf-nr1", tasks=["do nothing"], synthesis=None,
        mode="map_reduce", timeout_s=10,
    )

    p = await _wait_for_tool_result(queue)
    assert p.data["status"] == "no_report"


@pytest.mark.asyncio
async def test_n1_runtime_error(tmp_path: Path):
    """A crashing agent degrades to a status='error' report (raw for N=1)."""

    runner, queue, _ = _make_runner(FakeModel([RuntimeError("kaboom")]), tmp_path)
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
    model = FakeModel([bad_call] * 3)
    runner, queue, _ = _make_runner(model, tmp_path)
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
    model = FakeModel([_report_call("ok", "50 findings", long_details)])
    runner, queue, store = _make_runner(model, tmp_path)
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

    model = _HangModel()
    runner, queue, _ = _make_runner(model, tmp_path)
    runner.spawn(
        workflow_id="wf-hang1", tasks=["x"], synthesis=None,
        mode="map_reduce", timeout_s=60,
    )
    await asyncio.wait_for(model.entered.wait(), timeout=1.0)
    await asyncio.wait_for(runner.stop(), timeout=1.0)
    assert await _collect_tool_results(queue, settle=0.2) == []


@pytest.mark.asyncio
async def test_worker_uses_subagent_scaffolding(tmp_path: Path):
    """Worker agent's system prompt is the subagent scaffolding (no Identity)."""
    model = FakeModel([_report_call("ok", "x", "y")])
    runner, queue, _ = _make_runner(model, tmp_path)
    runner.spawn(
        workflow_id="wf-s1", tasks=["hello"], synthesis=None,
        mode="map_reduce", timeout_s=10,
    )
    await _wait_for_tool_result(queue)

    assert len(model.calls) == 1
    first, task = model.calls[0]["messages"]
    assert first["role"] == "system"
    system = first["content"]
    assert "worker agent" in system   # subagent_scaffolding template (renamed from "subagent")
    assert "Report" in system
    assert "# Identity" not in system
    assert "[Memory context]" not in system
    assert task == {"role": "user", "content": "hello"}
    assert model.calls[0]["purpose"] == "workflow"
    assert model.calls[0]["max_tokens"] == 4096
    assert model.calls[0]["think_budget"] == 1024


# ---------- Fan-out / concurrency / synthesis / verify ----------


@pytest.mark.asyncio
async def test_n_tasks_concurrent_all_collected(tmp_path: Path):
    """(a) N tasks run; no-synthesis roll-up collects every report."""
    model = _WorkerModel(hold=0.02)
    runner, queue, store = _make_runner(model, tmp_path)
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
    """(b) 12 tasks, Semaphore(8): peak concurrent model calls never > 8."""
    model = _WorkerModel(hold=0.1)
    runner, queue, _ = _make_runner(model, tmp_path)
    runner.spawn(
        workflow_id="wf-sem",
        tasks=[f"task-{i}" for i in range(12)],
        synthesis=None,
        mode="map_reduce",
        timeout_s=30,
    )

    p = await _wait_for_tool_result(queue, timeout=5.0)
    assert p.data["summary"] == "12 tasks: 12 ok, 0 not ok"
    assert 2 <= model.peak <= MAX_WORKFLOW_CONCURRENCY


@pytest.mark.asyncio
async def test_synthesis_combines(tmp_path: Path):
    """(c) Synthesis task text contains each worker summary; exactly one result."""
    model = _WorkerModel()
    runner, queue, _ = _make_runner(model, tmp_path)
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

    synth_calls = [c for c in model.calls if "MERGE_THEM" in _task_of(c)]
    assert len(synth_calls) == 1
    synth_text = _task_of(synth_calls[0])
    assert "task-AAA" in synth_text
    assert "task-BBB" in synth_text


@pytest.mark.asyncio
async def test_verify_mode_invocation_count(tmp_path: Path):
    """(d) mode=verify with synthesis → N task + N verify + 1 synthesis agents."""
    model = _WorkerModel()
    runner, queue, _ = _make_runner(model, tmp_path)
    runner.spawn(
        workflow_id="wf-verify",
        tasks=["v-1", "v-2"],
        synthesis="DONE",
        mode="verify",
        timeout_s=30,
    )

    await _wait_for_tool_result(queue)
    # 2 task agents + 2 verify skeptics + 1 synthesis agent.
    assert len(model.calls) == 2 + 2 + 1
    assert any("adversarial skeptic" in _task_of(c) for c in model.calls)


@pytest.mark.asyncio
async def test_n1_verify_skeptic_verdict_surfaces(tmp_path: Path):
    """M1 regression: N=1, mode=verify, synthesis=None → skeptic verdict folds into perception.

    Without the fix, the raw early-return discards the verify key; the perception
    is identical to a bare map_reduce N=1 result (no [verify] block in details).
    With the fix, _rollup is called so the skeptic's verdict is always visible.
    """
    worker_report = _report_call("ok", "task done", "task output")
    skeptic_report = _report_call("incomplete", "refutation found", "evidence against claim")
    model = FakeModel([worker_report, skeptic_report])
    runner, queue, _ = _make_runner(model, tmp_path)
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
    model = _WorkerModel()
    runner, queue, _ = _make_runner(model, tmp_path)
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
    model = _WorkerModel()
    runner, queue, _ = _make_runner(model, tmp_path)
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
    synth_calls = [c for c in model.calls if "SYNTH" in _task_of(c)]
    assert len(synth_calls) == 1
    synth_text = _task_of(synth_calls[0])
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
        model = FakeModel(
            ['<tool_call>{"name":"_CaptureTool","arguments":{"token":"x"}}</tool_call>']
        )
        runner, queue, _ = _make_runner(model, tmp_path)
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
    model = _WorkerModel(slow_hold=1.0)
    runner, queue, store = _make_runner(model, tmp_path)
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
    model = _WorkerModel(hold=0.01)
    runner, queue, _ = _make_runner(model, tmp_path)
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
    model = _WorkerModel(hold=0.02)
    runner, queue, _ = _make_runner(model, tmp_path)
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
        model = FakeModel(
            ['<tool_call>{"name":"_CaptureTool","arguments":{"token":"x"}}</tool_call>']
        )
        queue = PerceptionQueue()
        shell_runner = ShellRunner(
            cwd=tmp_path,
            perception_queue=queue,
            tool_output_store=ToolOutputStore(tmp_path / "shell_outputs"),
        )
        runner, _, _ = _make_runner(model, tmp_path, queue, shell_runner=shell_runner)
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
        model = FakeModel(
            ['<tool_call>{"name":"_CaptureTool","arguments":{"token":"x"}}</tool_call>']
        )
        queue = PerceptionQueue()
        monitor_runner = MonitorRunner(cwd=tmp_path, perception_queue=queue)
        runner, _, _ = _make_runner(model, tmp_path, queue, monitor_runner=monitor_runner)
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

    runner, queue, _ = _make_runner(_HangModel(), tmp_path)
    runner.spawn(
        workflow_id="wf-eager", tasks=["slow-task"], synthesis=None,
        mode="map_reduce", timeout_s=60,
    )
    # No await — check synchronously that nothing arrived yet.
    # drain(timeout_s=0.0) fires after one event-loop tick; the workflow
    # is still blocked on the hanging model at that point.
    drained = await queue.drain(timeout_s=0.0)
    assert [p for p in drained if p.kind == "ToolResultArrived"] == [], (
        "spawn() must be fire-and-forget; result must not appear before workflow completes"
    )
    await runner.stop()


# ---------- cascade_log callback ----------


@pytest.mark.asyncio
async def test_cascade_log_gets_agent_reasoning_and_speech(tmp_path: Path):
    """Each agent pass reaches cascade_log with its raw reasoning and speech
    as streamed, and its native tool calls with ids."""
    from tests._dispatcher_helpers import _FakeCascadeLogger

    model = FakeModel([
        "weighing it</think>all done" + _report_call("ok", "s", "d"),
    ])
    runner, queue, _ = _make_runner(model, tmp_path)
    log = _FakeCascadeLogger()
    runner._cascade_logger = log
    runner.spawn(
        workflow_id="wf-log", tasks=["log me"], synthesis=None,
        mode="map_reduce", timeout_s=10,
    )
    await _wait_for_tool_result(queue)

    [it] = log.iters
    assert it["reasoning"] == "weighing it"
    assert it["speech"] == "all done"
    assert [(tc["id"], tc["name"]) for tc in it["tool_calls"]] == [("call_0", "Report")]
    assert [r.status for r in it["results"]] == ["ok"]


# ---------- A worker's Shell waits for its own result (plan 2026-10-28 decision 2) ----------


@pytest.mark.asyncio
async def test_worker_shell_result_reaches_the_worker_not_doll(tmp_path: Path):
    """Shell then Report through the real WorkflowRunner / run_agent / Shell /
    ShellRunner: the worker's second model call sees the command's output as
    the Shell tool message, and Doll's queue gets only the one Workflow
    result (3.7f in docs/research/2026-10-28-v1-tool-use-acceptance.md)."""
    shell_call = json.dumps({
        "name": "Shell",
        "arguments": {
            "command": "printf 'only_in_a.log\\nconfig.yaml differs\\n'",
            "timeout_s": 10,
        },
    })
    model = FakeModel([
        f"<tool_call>{shell_call}</tool_call>",
        _report_call("ok", "2 differences", "only_in_a.log; config.yaml"),
    ])
    queue = PerceptionQueue()
    shell_runner = ShellRunner(
        cwd=tmp_path,
        perception_queue=queue,
        # The same store the workflow's agents read (kernel wires one store).
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
    )
    runner, _, _ = _make_runner(model, tmp_path, queue, shell_runner=shell_runner)
    runner.spawn(
        workflow_id="wf-diff", tasks=["diff a/ and b/"], synthesis=None,
        mode="map_reduce", timeout_s=30,
    )

    p = await _wait_for_tool_result(queue, timeout=5.0)
    assert p.data["tool"] == "Workflow"
    assert p.data["status"] == "ok"
    assert p.data["summary"] == "2 differences"
    assert await _collect_tool_results(queue, settle=0.3) == []

    assert len(model.calls) == 2
    shell_message = model.calls[1]["messages"][-1]
    assert shell_message["role"] == "tool"
    assert shell_message["content"].startswith("Shell done [ok]: exit 0")
    assert shell_message["content"].splitlines()[-2:] == [
        "only_in_a.log", "config.yaml differs",
    ]
    await shell_runner.stop()


def test_worker_system_prompt_states_the_per_agent_cap(tmp_path: Path):
    """The workers' scaffolding carries the cap their Shell timeouts must stay
    under (a timeout_s at the cap is refused, see test_tools)."""
    runner, _, _ = _make_runner(FakeModel([]), tmp_path)
    cap = workflow_mod.MAX_AGENT_TIMEOUT_S
    assert f"cut off after {cap} s" in runner._system
    assert f"`timeout_s` of {cap} or more is refused" in runner._system


@pytest.mark.asyncio
async def test_worker_shell_past_the_cap_is_refused_and_the_worker_reports(
    tmp_path: Path, monkeypatch,
):
    """A Shell timeout_s at or past the per-agent cap would have the worker
    cancelled mid-command with nothing kept; refused instead, the worker sees
    why and still Reports."""
    monkeypatch.setattr(workflow_mod, "MAX_AGENT_TIMEOUT_S", 2)
    marker = tmp_path / "ran"
    shell_call = json.dumps({
        "name": "Shell",
        "arguments": {"command": f"sleep 4; touch {marker}", "timeout_s": 10},
    })
    model = FakeModel([
        f"<tool_call>{shell_call}</tool_call>",
        _report_call("incomplete", "needs a longer build", "Shell refused"),
    ])
    queue = PerceptionQueue()
    shell_runner = ShellRunner(
        cwd=tmp_path, perception_queue=queue,
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
    )
    runner, _, _ = _make_runner(model, tmp_path, queue, shell_runner=shell_runner)
    runner.spawn(
        workflow_id="wf-build", tasks=["run the build"], synthesis=None,
        mode="map_reduce", timeout_s=30,
    )

    p = await _wait_for_tool_result(queue, timeout=5.0)
    assert p.data["tool"] == "Workflow"
    assert p.data["summary"] == "needs a longer build"
    assert len(model.calls) == 2
    shell_message = model.calls[1]["messages"][-1]
    assert shell_message["content"].startswith("Shell refused: timeout_s=10")
    assert not marker.exists()
    await shell_runner.stop()
