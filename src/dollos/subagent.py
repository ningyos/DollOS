"""SubagentRunner — spawn ephemeral worker tasks; result re-enters perception queue.

A subagent is a one-shot asyncio.Task with its own minimal sub-cascade
(SUB_TOOLS, no character, no memory context block, no naked-text output). It MUST call
the `Report` tool to terminate — Report.run side-effects the structured
outcome onto its ctx, and SubagentRunner converts that into a
Perception("ToolResultArrived") enqueued into PerceptionQueue.

Lifecycle:
    main cascade → SpawnSubagent.run → ctx.subagent_runner.spawn(...)
                                           → asyncio.create_task(_run_with_timeout)
                                                  → _run_cascade(task)
                                                       → emit Report
                                                  → returns dict
                                           → perception_queue.put(Perception(...))
    MindLoop drains queue → Doll sees ToolResultArrived perception
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from dollos.cascade import run_tool_cascade
from dollos.llm.templates import build_qwen3_think_tool_grammar
from dollos.mind.mind_ctx import MindCtx
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.sink_resolver import SinkResolver
from dollos.prompts import PromptRenderer
from dollos.tool_outputs import ToolOutputStore
from dollos.tools import SUB_TOOLS

if TYPE_CHECKING:
    from dollos.memory import FtsMemory

    from dollos.llm.adapter import LLMAdapter
    from dollos.mind.perception_queue import PerceptionQueue
    from dollos.monitor_runner import MonitorRunner
    from dollos.shell_runner import ShellRunner

logger = logging.getLogger(__name__)

SUBAGENT_PREVIEW_LINES = 15


class SubagentRunner:
    """Spawn-and-track set of background subagent tasks.

    PerceptionQueue is wired post-build via set_perception_queue (see kernel.py).
    """

    def __init__(
        self,
        *,
        adapter: "LLMAdapter",
        renderer: PromptRenderer,
        memory_root: Path,
        memsearch: "FtsMemory",
        transcripts_root: Path,
        perception_queue: "PerceptionQueue | None" = None,
        shell_runner: "ShellRunner | None" = None,
        monitor_runner: "MonitorRunner | None" = None,
        tool_output_store: ToolOutputStore,
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
        self._tools_by_name: dict[str, type] = {
            cls.__name__: cls for cls in SUB_TOOLS
        }
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    def set_perception_queue(self, queue: "PerceptionQueue") -> None:
        """Wire the perception queue. Called by kernel after build."""
        self._perception_queue = queue

    def spawn(
        self,
        *,
        sub_id: str,
        task: str,
        timeout_s: int,
        response_sink=None,  # kept for call-site compatibility; ignored
    ) -> None:
        """Schedule a subagent task. Returns immediately."""
        if self._stopping:
            logger.warning("subagent spawn ignored: runner stopping")
            return
        coro = self._run_with_timeout(sub_id, task, timeout_s)
        t = asyncio.create_task(coro, name=f"subagent-{sub_id}")
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        self._stopping = True
        if not self._tasks:
            return
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _run_with_timeout(
        self,
        sub_id: str,
        task: str,
        timeout_s: int,
    ) -> None:
        try:
            report = await asyncio.wait_for(
                self._run_cascade(task), timeout=timeout_s
            )
        except asyncio.CancelledError:
            # Runner.stop() — don't enqueue a result; just exit.
            raise
        except asyncio.TimeoutError:
            details_full = f"timeout was {timeout_s}s"
            details_id = self._tool_output_store.write(details_full)
            detail_lines = details_full.splitlines()
            preview = "\n".join(detail_lines[:SUBAGENT_PREVIEW_LINES])
            self._enqueue(Perception(
                kind="ToolResultArrived",
                t=time.time(),
                data={
                    "tool": "Subagent",
                    "task_id": sub_id,
                    "status": "timeout",
                    "summary": "subagent exceeded wall-clock timeout",
                    "details": preview,
                    "details_output_id": details_id,
                    "details_line_count": len(detail_lines),
                    "task": task,
                },
            ))
            return
        except Exception as e:
            logger.exception("subagent %s crashed", sub_id)
            details_full = str(e)
            details_id = self._tool_output_store.write(details_full)
            detail_lines = details_full.splitlines()
            preview = "\n".join(detail_lines[:SUBAGENT_PREVIEW_LINES])
            self._enqueue(Perception(
                kind="ToolResultArrived",
                t=time.time(),
                data={
                    "tool": "Subagent",
                    "task_id": sub_id,
                    "status": "error",
                    "summary": f"subagent crashed: {type(e).__name__}",
                    "details": preview,
                    "details_output_id": details_id,
                    "details_line_count": len(detail_lines),
                    "task": task,
                },
            ))
            return

        if report is None:
            details_full = ""
            details_id = self._tool_output_store.write(details_full)
            detail_lines = details_full.splitlines()
            preview = "\n".join(detail_lines[:SUBAGENT_PREVIEW_LINES])
            self._enqueue(Perception(
                kind="ToolResultArrived",
                t=time.time(),
                data={
                    "tool": "Subagent",
                    "task_id": sub_id,
                    "status": "no_report",
                    "summary": "subagent ended without calling Report",
                    "details": preview,
                    "details_output_id": details_id,
                    "details_line_count": len(detail_lines),
                    "task": task,
                },
            ))
        else:
            details_full = report["details"]
            details_id = self._tool_output_store.write(details_full)
            detail_lines = details_full.splitlines()
            preview = "\n".join(detail_lines[:SUBAGENT_PREVIEW_LINES])
            self._enqueue(Perception(
                kind="ToolResultArrived",
                t=time.time(),
                data={
                    "tool": "Subagent",
                    "task_id": sub_id,
                    "status": report["status"],
                    "summary": report["summary"],
                    "details": preview,
                    "details_output_id": details_id,
                    "details_line_count": len(detail_lines),
                    "task": task,
                },
            ))

    def _enqueue(self, perception: "Perception") -> None:
        if self._perception_queue is None:
            logger.error(
                "subagent %s result dropped: perception_queue not wired",
                perception.data.get("task_id", "?"),
            )
            return
        try:
            self._perception_queue.put(perception)
        except Exception:
            logger.exception(
                "perception_queue.put raised for subagent %s",
                perception.data.get("task_id", "?"),
            )

    async def _run_cascade(self, task: str) -> dict | None:
        """Run the sub-cascade. Returns Report args dict, or None if Report
        never called (cascade ended naturally / stuck-tool abort)."""
        grammar = build_qwen3_think_tool_grammar(SUB_TOOLS)
        system = self._renderer.render(
            "subagent_scaffolding",
            tool_registry=self._tools_by_name,
        )
        messages: list[dict] = [{"role": "user", "content": task}]

        # Build a fresh MindState for this subagent — private scratchpad,
        # task as focus, no mood history, no shared state with Doll's loop.
        sub_state = MindState(focus=task)

        # Empty SinkResolver: subagent has no user-facing sink.
        _dummy_resolver = SinkResolver()

        ctx = MindCtx(
            mind_state=sub_state,
            memsearch=self._memsearch,
            memory_root=self._memory_root,
            transcripts_root=self._transcripts_root,
            sink_resolver=_dummy_resolver,   # subagent never speaks to user
            tool_output_store=self._tool_output_store,
            shell_runner=self._shell_runner,
            subagent_runner=None,            # no recursion
            monitor_runner=self._monitor_runner,
            # subagent_report starts None; Report tool sets it to signal exit
        )

        def _check_early_exit(iter_num: int, ctx: MindCtx) -> bool:
            # Report fires as a side-effect tool (returns None → not in
            # results). If it ran, ctx.subagent_report is set; signal exit.
            return ctx.subagent_report is not None

        await run_tool_cascade(
            adapter=self._adapter,
            system=system,
            messages=messages,
            tools=SUB_TOOLS,
            tools_by_name=self._tools_by_name,
            ctx=ctx,
            grammar=grammar,
            sink=None,
            max_tokens=4096,
            check_early_exit=_check_early_exit,
        )

        return ctx.subagent_report
