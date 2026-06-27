"""WorkflowRunner — deterministic fan-out of worker agents → one result.

A workflow is a fire-and-forget ``asyncio.Task`` that fans ``N`` tasks out as
parallel worker agents (reusing ``agent_engine.run_agent``), optionally runs an
adversarial verify pass per task, then either rolls them up or runs a synthesis
agent. Exactly ONE ``ToolResultArrived(tool="Workflow")`` perception re-enters
the ``PerceptionQueue`` with the combined result — the intermediate worker noise
never reaches Doll's context.

Modes (v1):
    map_reduce — parallel fan-out + optional synthesis.
    verify     — each worker result gets an independent adversarial skeptic
                 pass before synthesis / roll-up.

Lifecycle (mirrors the retired SubagentRunner):
    main cascade → SpawnWorkflow.run → ctx.workflow_runner.spawn(...)
                                          → asyncio.create_task(_run_with_timeout)
                                               → _run_workflow → gather _run_one_task
                                                    → run_agent (task / verify)
                                               → synthesis / roll-up → dict
                                          → perception_queue.put(Perception(...))
    MindLoop drains queue → Doll sees one ToolResultArrived perception.

The single workflow concept subsumes the old subagent: N=1 with no synthesis is
the degenerate single-worker case and returns that worker's Report raw.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from dollos.agent_engine import run_agent
from dollos.mind.mind_state import Perception
from dollos.tools import SUB_TOOLS

if TYPE_CHECKING:
    from dollos.cascade.tool_loop import ToolResult
    from dollos.cascade_log import CascadeLogger
    from dollos.llm.adapter import LLMAdapter
    from dollos.memory import FtsMemory
    from dollos.mind.perception_queue import PerceptionQueue
    from dollos.monitor_runner import MonitorRunner
    from dollos.prompts import PromptRenderer
    from dollos.shell_runner import ShellRunner
    from dollos.tool_outputs import ToolOutputStore

logger = logging.getLogger(__name__)

# Per-workflow fan-out cap (shared by task + verify agents).
MAX_WORKFLOW_CONCURRENCY = 8
# Per-agent wall-clock cap so one hung agent cannot starve the rest.
MAX_AGENT_TIMEOUT_S = 300
# Preview lines carried inline in the result perception (full text → store).
WORKFLOW_PREVIEW_LINES = 15

# Statuses that mean a worker produced no usable Report (degraded).
_DEGRADED = frozenset({"timeout", "error", "no_report"})


class WorkflowRunner:
    """Spawn-and-track set of background workflow tasks.

    Same construction / lifecycle shape as the retired SubagentRunner so it is
    a drop-in for kernel wiring + ``stop()``. PerceptionQueue is wired post-build
    via ``set_perception_queue`` (see kernel.py).
    """

    def __init__(
        self,
        *,
        adapter: "LLMAdapter",
        renderer: "PromptRenderer",
        memory_root: Path,
        memsearch: "FtsMemory",
        transcripts_root: Path,
        perception_queue: "PerceptionQueue | None" = None,
        shell_runner: "ShellRunner | None" = None,
        monitor_runner: "MonitorRunner | None" = None,
        tool_output_store: "ToolOutputStore",
        cascade_logger: "CascadeLogger | None" = None,
    ) -> None:
        self._adapter = adapter
        self._renderer = renderer
        self._memory_root = memory_root
        self._memsearch = memsearch
        self._transcripts_root = transcripts_root
        self._perception_queue = perception_queue
        self._shell_runner = shell_runner
        self._monitor_runner = monitor_runner
        self._tool_output_store = tool_output_store
        self._cascade_logger = cascade_logger
        self._tools_by_name: dict[str, type] = {
            cls.__name__: cls for cls in SUB_TOOLS
        }
        # Rendered once and reused across task / verify / synthesis agents.
        self._system = self._renderer.render(
            "subagent_scaffolding",
            tool_registry=self._tools_by_name,
        )
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    def set_perception_queue(self, queue: "PerceptionQueue") -> None:
        """Wire the perception queue. Called by kernel after build."""
        self._perception_queue = queue

    # ------------------------------------------------------------------ #
    # Public spawn / lifecycle                                            #
    # ------------------------------------------------------------------ #

    def spawn(
        self,
        *,
        workflow_id: str,
        tasks: list[str],
        synthesis: str | None,
        mode: str,
        timeout_s: int,
        response_sink=None,  # kept for call-site compatibility; ignored
    ) -> None:
        """Schedule a workflow. Returns immediately (fire-and-forget)."""
        if self._stopping:
            logger.warning("workflow spawn ignored: runner stopping")
            return
        coro = self._run_with_timeout(workflow_id, tasks, synthesis, mode, timeout_s)
        t = asyncio.create_task(coro, name=f"workflow-{workflow_id}")
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        self._stopping = True
        if not self._tasks:
            return
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ------------------------------------------------------------------ #
    # Whole-workflow driver + result perception                          #
    # ------------------------------------------------------------------ #

    async def _run_with_timeout(
        self,
        workflow_id: str,
        tasks: list[str],
        synthesis: str | None,
        mode: str,
        timeout_s: int,
    ) -> None:
        n = len(tasks)
        try:
            report = await asyncio.wait_for(
                self._run_workflow(tasks, synthesis, mode), timeout=timeout_s
            )
        except asyncio.CancelledError:
            # Runner.stop() — don't enqueue a result; just exit.
            raise
        except asyncio.TimeoutError:
            self._emit(
                workflow_id, n, mode,
                status="timeout",
                summary="workflow exceeded wall-clock timeout",
                details_full=f"timeout was {timeout_s}s",
            )
            return
        except Exception as e:
            logger.exception("workflow %s crashed", workflow_id)
            self._emit(
                workflow_id, n, mode,
                status="error",
                summary=f"workflow crashed: {type(e).__name__}",
                details_full=str(e),
            )
            return

        if report is None:
            self._emit(
                workflow_id, n, mode,
                status="no_report",
                summary="workflow ended without a result",
                details_full="",
            )
        else:
            self._emit(
                workflow_id, n, mode,
                status=report["status"],
                summary=report["summary"],
                details_full=report["details"],
            )

    def _emit(
        self,
        workflow_id: str,
        n: int,
        mode: str,
        *,
        status: str,
        summary: str,
        details_full: str,
    ) -> None:
        """Build + enqueue the single ToolResultArrived perception.

        Identical shape/keys to the old subagent result so mind_prompt's
        _percep_body renders it; full details paged through the shared
        ToolOutputStore.
        """
        details_id = self._tool_output_store.write(details_full)
        detail_lines = details_full.splitlines()
        preview = "\n".join(detail_lines[:WORKFLOW_PREVIEW_LINES])
        self._enqueue(Perception(
            kind="ToolResultArrived",
            t=time.time(),
            data={
                "tool": "Workflow",
                "task_id": workflow_id,
                "status": status,
                "summary": summary,
                "details": preview,
                "details_output_id": details_id,
                "details_line_count": len(detail_lines),
                "task": f"workflow: {n} tasks, mode={mode}",
            },
        ))

    def _enqueue(self, perception: "Perception") -> None:
        if self._perception_queue is None:
            logger.error(
                "workflow %s result dropped: perception_queue not wired",
                perception.data.get("task_id", "?"),
            )
            return
        try:
            self._perception_queue.put(perception)
        except Exception:
            logger.exception(
                "perception_queue.put raised for workflow %s",
                perception.data.get("task_id", "?"),
            )

    # ------------------------------------------------------------------ #
    # Fan-out → collect → synthesize / roll-up                           #
    # ------------------------------------------------------------------ #

    async def _run_workflow(
        self,
        tasks: list[str],
        synthesis: str | None,
        mode: str,
    ) -> dict | None:
        sem = asyncio.Semaphore(MAX_WORKFLOW_CONCURRENCY)
        reports = await asyncio.gather(
            *[self._run_one_task(i, t, mode, sem) for i, t in enumerate(tasks)]
        )

        if synthesis is not None:
            synth = await self._run_synthesis(synthesis, reports)
            if synth is not None:
                synth = dict(synth)
                synth["status"] = self._rollup_status(reports, synth)
            return synth

        # No synthesis.
        if len(reports) == 1:
            # Degenerate single-worker case → that worker's Report raw.
            return reports[0]
        return self._rollup(reports)

    async def _run_one_task(
        self,
        index: int,
        task: str,
        mode: str,
        sem: asyncio.Semaphore,
    ) -> dict:
        """Run one task agent (semaphore-bounded) + optional verify pass.

        Always returns a report dict (never None); degraded states are
        first-class via status in {timeout, error, no_report}. The verify
        skeptic re-acquires the semaphore separately so it never deadlocks
        against task agents holding all permits.
        """
        async with sem:
            primary = await self._run_capped(task)
        primary = dict(primary)
        primary["_index"] = index
        primary["_task"] = task

        if mode == "verify" and primary["status"] not in _DEGRADED:
            async with sem:
                verify = await self._run_capped(self._verify_prompt(task, primary))
            primary["verify"] = verify
        return primary

    async def _run_capped(self, task: str) -> dict:
        """Run one agent with the per-agent timeout cap. Always a report dict."""
        try:
            report = await asyncio.wait_for(
                self._spawn_agent(task), timeout=MAX_AGENT_TIMEOUT_S
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return {
                "status": "timeout",
                "summary": "agent exceeded per-agent timeout",
                "details": f"per-agent timeout was {MAX_AGENT_TIMEOUT_S}s",
            }
        except Exception as e:
            logger.exception("workflow agent crashed")
            return {
                "status": "error",
                "summary": f"agent crashed: {type(e).__name__}",
                "details": str(e),
            }
        if report is None:
            return {
                "status": "no_report",
                "summary": "agent ended without calling Report",
                "details": "",
            }
        return report

    async def _spawn_agent(self, task: str) -> dict | None:
        return await run_agent(
            task=task,
            system=self._system,
            adapter=self._adapter,
            renderer=self._renderer,
            memory_root=self._memory_root,
            memsearch=self._memsearch,
            transcripts_root=self._transcripts_root,
            tool_output_store=self._tool_output_store,
            shell_runner=self._shell_runner,
            monitor_runner=self._monitor_runner,
            tools=SUB_TOOLS,
            max_tokens=4096,
            on_iter_end=self._make_on_iter_end(),
        )

    def _make_on_iter_end(self):
        """Per-agent cascade logger callback (fresh turn_id per agent) so the
        live cascade_log shows the fan-out. None when no logger is wired."""
        if self._cascade_logger is None:
            return None
        turn_id = self._cascade_logger.start_turn()

        def _cb(
            iter_num: int,
            assistant_text: str,
            tool_calls: "list[dict]",
            results: "list[ToolResult]",
            duration_ms: int,
        ) -> None:
            try:
                self._cascade_logger.log_iter(
                    turn_id=turn_id,
                    iter=iter_num,
                    assistant_text=assistant_text,
                    tool_calls=tool_calls,
                    results=results,
                    duration_ms=duration_ms,
                )
            except Exception:
                logger.exception("workflow cascade log_iter failed; continuing")

        return _cb

    # ------------------------------------------------------------------ #
    # Verify / synthesis / roll-up                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _verify_prompt(task: str, report: dict) -> str:
        """Adversarial skeptic prompt — default to refuted if unsubstantiated."""
        return (
            "You are an adversarial skeptic verifying another worker agent's "
            "result. The worker was given this task:\n\n"
            f"{task}\n\n"
            "It produced this result:\n\n"
            f"status: {report['status']}\n"
            f"summary: {report['summary']}\n"
            f"details:\n{report['details']}\n\n"
            "Try to REFUTE this result. Independently re-check it (re-run "
            "commands, re-read files, recompute). If you cannot substantiate a "
            "claim, treat it as refuted — default to refuted when evidence is "
            "missing. Then call Report: status=ok if the result holds up, "
            "status=incomplete if you found problems; summary = your one-sentence "
            "verdict; details = what you checked and what you found."
        )

    async def _run_synthesis(
        self,
        synthesis: str,
        reports: list[dict],
    ) -> dict | None:
        """Run the synthesis agent over the collected reports.

        Each report is rendered as a bounded block (status | summary |
        details-preview + a ReadToolOutput id pointing at the full details in
        the SHARED ToolOutputStore the synthesis agent can page). The synthesis
        framing is prepended to the agent's task; its Report becomes the result.
        """
        parts: list[str] = [
            "You are the synthesis agent for a workflow. Combine the worker "
            "results below into a single Report, following this instruction:\n",
            synthesis,
            "",
            "Worker results:",
        ]
        for r in reports:
            idx = r.get("_index", "?")
            details_full = r["details"]
            out_id = self._tool_output_store.write(details_full)
            lines = details_full.splitlines()
            preview = "\n".join(lines[:WORKFLOW_PREVIEW_LINES])
            shown = min(len(lines), WORKFLOW_PREVIEW_LINES)
            block = (
                f"\n[task {idx}] status={r['status']} | {r['summary']}\n"
                f"details preview ({shown}/{len(lines)} lines):\n{preview}\n"
                f"(full details: ReadToolOutput id={out_id})"
            )
            if "verify" in r:
                v = r["verify"]
                block += (
                    f"\n[task {idx} verify] status={v['status']} | {v['summary']}"
                )
            parts.append(block)
        parts.append(
            "\nNow call Report with status/summary/details combining the above."
        )
        synthesis_task = "\n".join(parts)
        return await self._run_capped(synthesis_task)

    def _rollup(self, reports: list[dict]) -> dict:
        """Deterministic roll-up Report for N>1 with no synthesis (no LLM call)."""
        status = self._rollup_status(reports, None)
        n = len(reports)
        ok = sum(1 for r in reports if r["status"] == "ok")
        summary = f"{n} tasks: {ok} ok, {n - ok} not ok"
        blocks: list[str] = []
        for r in reports:
            idx = r.get("_index", "?")
            block = f"[task {idx}] {r['status']} | {r['summary']}\n{r['details']}"
            if "verify" in r:
                v = r["verify"]
                block += f"\n  [verify] {v['status']} | {v['summary']}"
            blocks.append(block)
        return {"status": status, "summary": summary, "details": "\n\n".join(blocks)}

    @staticmethod
    def _rollup_status(reports: list[dict], synthesis_report: dict | None) -> str:
        """Single documented status helper (locked decision).

        ok iff synthesis (or, with no synthesis, all tasks) returned ok; any
        task timeout/error/no_report degrades the whole workflow to incomplete.
        """
        if any(r["status"] in _DEGRADED for r in reports):
            return "incomplete"
        if synthesis_report is not None:
            return synthesis_report["status"]
        return "ok" if all(r["status"] == "ok" for r in reports) else "incomplete"
