"""Shared LLM tool cascade loop.

Both EventDispatcher._respond and SubagentRunner._run_cascade implement
the same parser-feed + tool-dispatch + tool_response + stuck-tool 3-strike
loop. This module lifts that shared logic into run_tool_cascade and
dispatch_tool_call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from dollos.ipc.messages import ErrorMsg, ServerMessage
from dollos.tool_parser import ToolStreamParser

if TYPE_CHECKING:
    from dollos.llm.adapter import LLMAdapter
    from dollos.mind.mind_ctx import MindCtx

logger = logging.getLogger(__name__)


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


def format_unknown_tool(name: str, registry: dict[str, type]) -> str:
    """LLM-friendly unknown-tool message listing the valid tool names."""
    available = ", ".join(sorted(registry)) if registry else "(none)"
    return f"未知工具 {name!r}。可用工具：{available}"


def format_validation_error(exc: ValidationError, tool_name: str) -> str:
    """Flatten a pydantic ValidationError into a terse, actionable message.

    One line per bad field: ``<field>: <msg> (你給了 <value>)``. Avoids the raw
    pydantic error wall (URLs / 'N validation errors for ...') which costs
    tokens and reads as noise to the model.
    """
    lines: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        msg = err.get("msg", "invalid")
        given = err.get("input", None)
        lines.append(f"{loc}: {msg}（你給了 {given!r}）")
    body = "; ".join(lines)
    return f"工具 {tool_name} 參數錯誤：{body}"


async def dispatch_one(
    name: str,
    arguments: dict,
    ctx: "MindCtx",
    registry: dict[str, type],
) -> ToolResult | None:
    """Validate + run one tool call. Single source of truth for both the live
    MindLoop and the subagent cascade (spec §3.6).

    Returns None for side-effect tools (run() -> None); ToolResult otherwise.
    Friendly messages via format_unknown_tool / format_validation_error.
    """
    tool_cls = registry.get(name)
    if tool_cls is None:
        logger.warning("unknown tool: %r", name)
        return ToolResult(
            tool_name=name, success=False, detail=format_unknown_tool(name, registry)
        )
    try:
        tool = tool_cls.model_validate(arguments)
    except ValidationError as e:
        logger.warning("tool args validation failed for %s: %s", name, e)
        return ToolResult(
            tool_name=name, success=False, detail=format_validation_error(e, name)
        )
    try:
        returned = await tool.run(ctx)
    except Exception as e:
        logger.exception("tool %s raised", name)
        return ToolResult(
            tool_name=name, success=False, detail=f"runtime error: {e}"
        )
    if returned is None:
        return None
    return ToolResult(tool_name=name, success=True, detail=returned)


async def dispatch_tool_call(
    call: dict,
    ctx: "MindCtx",
    tools_by_name: dict[str, type],
) -> ToolResult | None:
    """Thin wrapper over dispatch_one for the run_tool_cascade call path."""
    name = call.get("name")
    if not isinstance(name, str):
        return ToolResult(
            tool_name=str(name),
            success=False,
            detail="missing or non-string 'name' field in tool_call",
        )
    return await dispatch_one(
        name, call.get("arguments", {}) or {}, ctx, tools_by_name,
    )


async def run_tool_cascade(
    *,
    adapter: "LLMAdapter",
    system: str,
    messages: list[dict],
    tools: list[type],
    tools_by_name: dict[str, type],
    ctx: "MindCtx",
    grammar: str,
    sink: "asyncio.Queue[ServerMessage | None] | None",
    max_tokens: int,
    on_iter_start: Callable[[int, list[dict]], None] | None = None,
    on_iter_end: Callable[[int, str, list[dict], list[ToolResult], int], None] | None = None,
    check_early_exit: Callable[[int, "MindCtx"], bool] | None = None,
) -> list[dict]:
    """Run an LLM tool cascade. Mutates `messages` in place; also returns it.

    on_iter_start fires before each iter — receives (iter_num, messages).
        Caller can append [Pending events] / [Active monitors] blocks
        here for iter > 1.
    on_iter_end fires after each iter — receives (iter_num, assistant_text,
        parsed_tool_calls, results, duration_ms). Caller logs to
        CascadeLogger here.
    check_early_exit fires after the assistant message is appended but before
        result processing. If it returns True, the cascade exits immediately
        (used by subagent to detect Report side-effect).
    Returns the final messages list (same object, in-place mutated, returned
    for ergonomics).
    """
    consecutive_fails: dict[str, int] = {}
    iter_num = 0

    while True:
        iter_num += 1
        iter_start = time.monotonic()

        if on_iter_start is not None:
            on_iter_start(iter_num, messages)

        parser = ToolStreamParser()
        results: list[ToolResult] = []
        assistant_buf: list[str] = []
        parsed_tool_calls: list[dict] = []

        async for chunk in adapter.stream_messages(
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            grammar=grammar,
        ):
            if chunk.text:
                assistant_buf.append(chunk.text)
            for call in parser.feed(chunk.text):
                parsed_tool_calls.append(call)
                result = await dispatch_tool_call(call, ctx, tools_by_name)
                if result is not None:
                    results.append(result)
            if chunk.done:
                break
        for call in parser.flush():
            parsed_tool_calls.append(call)
            result = await dispatch_tool_call(call, ctx, tools_by_name)
            if result is not None:
                results.append(result)

        # Append the model's full raw emit as the assistant turn.
        messages.append({
            "role": "assistant",
            "content": "".join(assistant_buf),
        })

        duration_ms = int((time.monotonic() - iter_start) * 1000)
        if on_iter_end is not None:
            on_iter_end(iter_num, "".join(assistant_buf), parsed_tool_calls, results, duration_ms)

        # Allow caller to exit early (e.g. subagent Report side-effect).
        if check_early_exit is not None and check_early_exit(iter_num, ctx):
            break

        if not results:
            break

        # Append a tool_response user message for each tool result.
        for r in results:
            detail = r.detail if r.detail else "(no output)"
            messages.append({
                "role": "user",
                "content": f"<tool_response>\n{detail}\n</tool_response>",
            })

        # Update per-tool failure counts; clear all on any success.
        # Per-tool counts accumulate across tool changes so that alternating
        # failing tools cannot run the cascade unbounded (I3 fix).
        for r in results:
            if r.success:
                consecutive_fails.clear()
            else:
                consecutive_fails[r.tool_name] = (
                    consecutive_fails.get(r.tool_name, 0) + 1
                )

        stuck_tool = next(
            (n for n, c in consecutive_fails.items() if c >= 3),
            None,
        )
        if stuck_tool is not None:
            if sink is not None:
                sink.put_nowait(ErrorMsg(
                    message=(
                        f"cascade aborted: 連續 3 次 {stuck_tool} tool 失敗，"
                        f"停下來換思路。"
                    )
                ))
            else:
                logger.info(
                    "cascade stuck on %s (3x); aborting", stuck_tool
                )
            break

    return messages
