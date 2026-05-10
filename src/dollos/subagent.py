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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from dollos.events import RawEvent, SubagentResultEvent
from dollos.ipc.messages import ServerMessage
from dollos.llm.templates import build_qwen3_think_tool_grammar
from dollos.process_registry import ProcessRegistry
from dollos.prompts import PromptRenderer
from dollos.tool_parser import ToolStreamParser
from dollos.tools import SUB_TOOLS, ToolCtx

if TYPE_CHECKING:
    from memsearch import MemSearch

    from dollos.llm.adapter import LLMAdapter

logger = logging.getLogger(__name__)


class SubagentRunner:
    """Spawn-and-track set of background subagent tasks.

    Built before the dispatcher (chicken-and-egg: SpawnSubagent.run needs
    a runner; SubagentRunner needs to fire events into the dispatcher).
    The dispatch sink is provided as a callable (`dispatch_fn`) which the
    kernel sets to `dispatcher.dispatch` after the dispatcher is built.
    Until set, runner falls back to logging an error.
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
        process_registry: ProcessRegistry | None = None,
    ) -> None:
        self._adapter = adapter
        self._renderer = renderer
        self._memory_root = memory_root
        self._memsearch = memsearch
        self._transcripts_root = transcripts_root
        self._dispatch_fn = dispatch_fn
        self._process_registry = process_registry
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
            event = SubagentResultEvent(
                subagent_id=sub_id,
                task=task,
                status="timeout",
                summary="subagent exceeded wall-clock timeout",
                details=f"timeout was {timeout_s}s",
                response_sink=response_sink,
            )
        except Exception as e:
            logger.exception("subagent %s crashed", sub_id)
            event = SubagentResultEvent(
                subagent_id=sub_id,
                task=task,
                status="error",
                summary=f"subagent crashed: {type(e).__name__}",
                details=str(e),
                response_sink=response_sink,
            )
        else:
            if report is None:
                event = SubagentResultEvent(
                    subagent_id=sub_id,
                    task=task,
                    status="no_report",
                    summary="subagent ended without calling Report",
                    details="",
                    response_sink=response_sink,
                )
            else:
                event = SubagentResultEvent(
                    subagent_id=sub_id,
                    task=task,
                    status=report["status"],
                    summary=report["summary"],
                    details=report["details"],
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

        ctx = ToolCtx(
            sink=None,  # subagent has no live user sink
            memory_root=self._memory_root,
            memsearch=self._memsearch,
            transcripts_root=self._transcripts_root,
            subagent_runner=None,  # no recursion
            subagent_report=None,
            process_registry=self._process_registry,
        )

        consecutive_fails: dict[str, int] = {}
        last_failed_tool: str | None = None

        while True:
            parser = ToolStreamParser()
            results: list[_SubToolResult] = []
            assistant_buf: list[str] = []

            async for chunk in self._adapter.stream_messages(
                system=system,
                messages=messages,
                tools=SUB_TOOLS,
                max_tokens=4096,
                grammar=grammar,
            ):
                if chunk.text:
                    assistant_buf.append(chunk.text)
                for call in parser.feed(chunk.text):
                    r = await self._dispatch_tool(call, ctx)
                    if r is not None:
                        results.append(r)
                if chunk.done:
                    break
            for call in parser.flush():
                r = await self._dispatch_tool(call, ctx)
                if r is not None:
                    results.append(r)

            messages.append(
                {"role": "assistant", "content": "".join(assistant_buf)}
            )

            # Report fires as a side-effect tool (returns None → not in
            # results). If it ran, ctx.subagent_report is set; ship it.
            if ctx.subagent_report is not None:
                return ctx.subagent_report

            if not results:
                # No tool calls and no Report → cascade ended unproductively.
                return None

            for r in results:
                detail = r.detail if r.detail else "(no output)"
                messages.append(
                    {
                        "role": "user",
                        "content": f"<tool_response>\n{detail}\n</tool_response>",
                    }
                )

            for r in results:
                if r.success:
                    consecutive_fails.clear()
                    last_failed_tool = None
                else:
                    if r.tool_name == last_failed_tool:
                        consecutive_fails[r.tool_name] = (
                            consecutive_fails.get(r.tool_name, 1) + 1
                        )
                    else:
                        last_failed_tool = r.tool_name
                        consecutive_fails = {r.tool_name: 1}

            stuck_tool = next(
                (n for n, c in consecutive_fails.items() if c >= 3),
                None,
            )
            if stuck_tool is not None:
                logger.info(
                    "subagent stuck on %s (3x); aborting without Report", stuck_tool
                )
                return None

    async def _dispatch_tool(
        self, call: dict, ctx: ToolCtx
    ) -> "_SubToolResult | None":
        """Mirror EventDispatcher._dispatch_tool_call but with SUB_TOOLS lookup
        and no sink-pushed ErrorMsg (subagent has no live sink)."""
        name = call.get("name")
        if not isinstance(name, str):
            return _SubToolResult(
                tool_name=str(name),
                success=False,
                detail="missing or non-string 'name' field in tool_call",
            )
        tool_cls = self._tools_by_name.get(name)
        if tool_cls is None:
            logger.warning("subagent unknown tool: %r", name)
            return _SubToolResult(
                tool_name=name, success=False, detail="unknown tool"
            )
        try:
            tool = tool_cls.model_validate(call.get("arguments", {}))
        except ValidationError as e:
            logger.warning("subagent tool args validation failed for %s: %s", name, e)
            return _SubToolResult(
                tool_name=name,
                success=False,
                detail=f"args validation: {e}",
            )
        try:
            returned = await tool.run(ctx)
        except Exception as e:
            logger.exception("subagent tool %s raised", name)
            return _SubToolResult(
                tool_name=name,
                success=False,
                detail=f"runtime error: {e}",
            )
        if returned is None:
            return None
        return _SubToolResult(tool_name=name, success=True, detail=returned)


# Internal mirror of dispatcher.ToolResult (kept private to avoid coupling).
@dataclass
class _SubToolResult:
    tool_name: str
    success: bool
    detail: str
