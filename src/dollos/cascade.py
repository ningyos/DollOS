"""Shared LLM tool cascade loop.

Both EventDispatcher._respond and SubagentRunner._run_cascade implement
the same parser-feed + tool-dispatch + tool_response + stuck-tool 3-strike
loop. This module lifts that shared logic into run_tool_cascade and
dispatch_tool_call.

ErrorMsg push on runtime exception only fires when ctx.sink is not None
(subagents have no live sink).
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
    from dollos.tools import ToolCtx

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


async def dispatch_tool_call(
    call: dict,
    ctx: "ToolCtx",
    tools_by_name: dict[str, type],
) -> ToolResult | None:
    """Execute a single tool call. Returns ToolResult if cascade-worthy, None otherwise.

    Returns None when:
      - tool.run() returned None (side-effect tool, no cascade)
    Returns ToolResult when:
      - validation/unknown error (success=False, error in detail)
      - runtime exception (success=False, error in detail) — also pushes
        ErrorMsg to sink when ctx.sink is not None
      - tool.run() returned str (success=True, str in detail; may be empty)
    """
    name = call.get("name")
    if not isinstance(name, str):
        return ToolResult(
            tool_name=str(name),
            success=False,
            detail="missing or non-string 'name' field in tool_call",
        )
    tool_cls = tools_by_name.get(name)
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
        if ctx.sink is not None:
            ctx.sink.put_nowait(ErrorMsg(message=f"tool {name} error: {e}"))
        return ToolResult(
            tool_name=name, success=False, detail=f"runtime error: {e}"
        )
    if returned is None:
        return None
    return ToolResult(tool_name=name, success=True, detail=returned)


async def run_tool_cascade(
    *,
    adapter: "LLMAdapter",
    system: str,
    messages: list[dict],
    tools: list[type],
    tools_by_name: dict[str, type],
    ctx: "ToolCtx",
    grammar: str,
    sink: "asyncio.Queue[ServerMessage | None] | None",
    max_tokens: int,
    on_iter_start: Callable[[int, list[dict]], None] | None = None,
    on_iter_end: Callable[[int, str, list[dict], list[ToolResult], int], None] | None = None,
    check_early_exit: Callable[[int, "ToolCtx"], bool] | None = None,
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
    last_failed_tool: str | None = None
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

        # Update same-tool consecutive-failure tracker.
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
