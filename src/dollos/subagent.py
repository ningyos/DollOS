"""SubagentRunner — spawn ephemeral worker tasks; result re-enters event queue.

Plan: `docs/superpowers/plans/2026-05-09-subagent.md`.

A subagent is a one-shot asyncio.Task with its own minimal sub-cascade
(SUB_TOOLS, no character, no memory context block, no Say). It MUST call
the `Report` tool to terminate — Report.run side-effects the structured
outcome onto its ctx, and SubagentRunner converts that into a
SubagentResultEvent dispatched back through the main EventDispatcher.

Lifecycle:
    main cascade → SpawnSubagent.run → ctx.subagent_runner.spawn(...)
                                           → asyncio.create_task(_run_with_timeout)
                                                  → _run_cascade(task)
                                                       → emit Report
                                                  → returns dict
                                           → dispatch_fn(SubagentResultEvent)
    main cascade → main turn fires for the result → Doll sees perception
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from dollos.cascade import run_tool_cascade
from dollos.events import RawEvent, SubagentResultEvent
from dollos.ipc.messages import ServerMessage
from dollos.llm.templates import build_qwen3_think_tool_grammar
from dollos.prompts import PromptRenderer
from dollos.tool_outputs import ToolOutputStore
from dollos.tools import SUB_TOOLS, SubagentToolCtx, ToolCtx

if TYPE_CHECKING:
    from memsearch import MemSearch

    from dollos.llm.adapter import LLMAdapter
    from dollos.monitor_runner import MonitorRunner
    from dollos.shell_runner import ShellRunner

logger = logging.getLogger(__name__)

SUBAGENT_PREVIEW_LINES = 15


class SubagentRunner:
    """Spawn-and-track set of background subagent tasks.

    Dispatch sink is wired post-build via set_dispatch_fn (see kernel.py).
    """

    def __init__(
        self,
        *,
        adapter: "LLMAdapter",
        renderer: PromptRenderer,
        memory_root: Path,
        memsearch: "MemSearch",
        transcripts_root: Path,
        dispatch_fn: Callable[[RawEvent], None] | None = None,
        shell_runner: "ShellRunner | None" = None,
        monitor_runner: "MonitorRunner | None" = None,
        tool_output_store: ToolOutputStore,
    ) -> None:
        self._adapter = adapter
        self._renderer = renderer
        self._memory_root = memory_root
        self._memsearch = memsearch
        self._transcripts_root = transcripts_root
        self._dispatch_fn = dispatch_fn
        self._shell_runner = shell_runner
        self._monitor_runner = monitor_runner
        self._tool_output_store = tool_output_store
        self._tools_by_name: dict[str, type] = {
            cls.__name__: cls for cls in SUB_TOOLS
        }
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    def set_dispatch_fn(self, fn: Callable[[RawEvent], None]) -> None:
        """Wire the result-event sink. Called by kernel after dispatcher build."""
        self._dispatch_fn = fn

    def spawn(
        self,
        *,
        sub_id: str,
        task: str,
        timeout_s: int,
        response_sink: asyncio.Queue[ServerMessage | None] | None,
    ) -> None:
        """Schedule a subagent task. Returns immediately."""
        if self._stopping:
            logger.warning("subagent spawn ignored: runner stopping")
            return
        coro = self._run_with_timeout(sub_id, task, timeout_s, response_sink)
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
        response_sink: asyncio.Queue[ServerMessage | None] | None,
    ) -> None:
        try:
            report = await asyncio.wait_for(
                self._run_cascade(task), timeout=timeout_s
            )
        except asyncio.CancelledError:
            # Runner.stop() — don't dispatch a result event; just exit.
            raise
        except asyncio.TimeoutError:
            details_full = f"timeout was {timeout_s}s"
            details_id = self._tool_output_store.write(details_full)
            detail_lines = details_full.splitlines()
            preview = "\n".join(detail_lines[:SUBAGENT_PREVIEW_LINES])
            event = SubagentResultEvent(
                subagent_id=sub_id,
                task=task,
                status="timeout",
                summary="subagent exceeded wall-clock timeout",
                details=preview,
                details_output_id=details_id,
                details_line_count=len(detail_lines),
                response_sink=response_sink,
            )
        except Exception as e:
            logger.exception("subagent %s crashed", sub_id)
            details_full = str(e)
            details_id = self._tool_output_store.write(details_full)
            detail_lines = details_full.splitlines()
            preview = "\n".join(detail_lines[:SUBAGENT_PREVIEW_LINES])
            event = SubagentResultEvent(
                subagent_id=sub_id,
                task=task,
                status="error",
                summary=f"subagent crashed: {type(e).__name__}",
                details=preview,
                details_output_id=details_id,
                details_line_count=len(detail_lines),
                response_sink=response_sink,
            )
        else:
            if report is None:
                details_full = ""
                details_id = self._tool_output_store.write(details_full)
                detail_lines = details_full.splitlines()
                preview = "\n".join(detail_lines[:SUBAGENT_PREVIEW_LINES])
                event = SubagentResultEvent(
                    subagent_id=sub_id,
                    task=task,
                    status="no_report",
                    summary="subagent ended without calling Report",
                    details=preview,
                    details_output_id=details_id,
                    details_line_count=len(detail_lines),
                    response_sink=response_sink,
                )
            else:
                details_full = report["details"]
                details_id = self._tool_output_store.write(details_full)
                detail_lines = details_full.splitlines()
                preview = "\n".join(detail_lines[:SUBAGENT_PREVIEW_LINES])
                event = SubagentResultEvent(
                    subagent_id=sub_id,
                    task=task,
                    status=report["status"],
                    summary=report["summary"],
                    details=preview,
                    details_output_id=details_id,
                    details_line_count=len(detail_lines),
                    response_sink=response_sink,
                )
        self._fire(event)

    def _fire(self, event: SubagentResultEvent) -> None:
        if self._dispatch_fn is None:
            logger.error(
                "subagent %s finished but no dispatch_fn wired; event dropped",
                event.subagent_id,
            )
            return
        try:
            self._dispatch_fn(event)
        except Exception:
            logger.exception(
                "dispatch of SubagentResultEvent for %s failed",
                event.subagent_id,
            )

    async def _run_cascade(self, task: str) -> dict | None:
        """Run the sub-cascade. Returns Report args dict, or None if Report
        never called (cascade ended naturally / stuck-tool abort)."""
        grammar = build_qwen3_think_tool_grammar(SUB_TOOLS)
        system = self._renderer.render("subagent_scaffolding")
        messages: list[dict] = [{"role": "user", "content": task}]

        ctx = SubagentToolCtx(
            sink=None,  # subagent has no live user sink
            memory_root=self._memory_root,
            memsearch=self._memsearch,
            transcripts_root=self._transcripts_root,
            subagent_runner=None,  # no recursion
            shell_runner=self._shell_runner,
            monitor_runner=self._monitor_runner,
            tool_output_store=self._tool_output_store,
        )

        def _check_early_exit(iter_num: int, ctx: ToolCtx) -> bool:
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
