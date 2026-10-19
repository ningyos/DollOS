"""Shared tool dispatch + the agent tool cascade.

``dispatch_one`` validates and runs one call and ``run_tool_calls`` runs one
reply's native tool calls in order; both MindLoop and ``run_tool_cascade``
(the agent loop under ``agent_engine.run_agent``) use them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from dollos.ipc.messages import ErrorMsg, ServerMessage
from dollos.llm.model import End, SpeechDelta, ThinkDelta, ToolCall, ToolCalls, tool_message
from dollos.llm.tool_schema import tool_schemas

if TYPE_CHECKING:
    from dollos.llm.model import ModelAdapter
    from dollos.mind.mind_ctx import MindCtx

logger = logging.getLogger(__name__)

# An agent that keeps calling tools without ever ending stops here.
MAX_CASCADE_PASSES = 16

# A tool message stays in the conversation (plan 2026-10-18 Task 8), so one
# longer than this is cut. InvokeSkill is exempt: a skill is the full text
# she has to follow and cannot be paged through.
TOOL_MESSAGE_MAX_CHARS = 8000
UNCUT_TOOLS = frozenset({"InvokeSkill"})

# The tool message of a call that never ran because the turn was interrupted
# (plan 2026-10-18 Task 9): every call in a reply must have its tool message.
NOT_RUN = "not run: turn interrupted"


@dataclass
class ToolResult:
    """Tool execution result. Internal cascade primitive (not a RawEvent).

    success=False: mechanical fail (validation / unknown / runtime exception).
    success=True:  ran cleanly. detail = the str returned by Tool.run().
                   May be empty string (Tool ran but had no content to return).

    Failures always cascade (Doll should fix). Successes cascade iff
    Tool.run() returned a str (not None) — i.e., the tool author opted in.

    unbound=True: the tool is in the schema the model sees but not bound
    this turn, so the call was refused without running. Not a failure.
    """

    tool_name: str
    success: bool
    detail: str
    unbound: bool = False

    @property
    def status(self) -> str:
        if self.unbound:
            return "rejected_unbound"
        return "ok" if self.success else "failed"


@dataclass(frozen=True)
class ToolOutcome:
    """One executed native tool call: the call, its result, and the ``tool``
    message that answers it."""

    call: ToolCall
    result: ToolResult
    message: dict


def format_unknown_tool(name: str, registry: dict[str, type]) -> str:
    """LLM-friendly unknown-tool message listing the valid tool names."""
    available = ", ".join(sorted(registry)) if registry else "(none)"
    return f"Unknown tool {name!r}. Available tools: {available}"


def format_validation_error(exc: ValidationError, tool_name: str) -> str:
    """Flatten a pydantic ValidationError into a terse, actionable message.

    One line per bad field: ``<field>: <msg> (you gave <value>)``. Avoids the raw
    pydantic error wall (URLs / 'N validation errors for ...') which costs
    tokens and reads as noise to the model.
    """
    lines: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        msg = err.get("msg", "invalid")
        given = err.get("input", None)
        lines.append(f"{loc}: {msg} (you gave {given!r})")
    body = "; ".join(lines)
    return f"Tool {tool_name} got bad arguments: {body}"


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


async def run_tool_calls(
    calls: Sequence[ToolCall],
    dispatch: Callable[[str, dict], Awaitable[ToolResult | None]],
    stop: Callable[[], bool] | None = None,
) -> list[ToolOutcome]:
    """Run one reply's tool calls in index order, one ``tool`` message each,
    in the same order (the server template matches results to calls by order
    only).

    ``arguments`` is the raw JSON string from the server; it must parse to an
    object, else the call fails without running. A tool that returns None
    answers "ok"; a failure answers with its error text. A message over
    ``TOOL_MESSAGE_MAX_CHARS`` is cut (``UNCUT_TOOLS`` excepted); the
    ``ToolResult`` keeps the full text.

    ``stop()`` is checked before each call; once it is true, that call and
    the rest do not run and answer ``NOT_RUN``.
    """
    outcomes: list[ToolOutcome] = []
    for call in calls:
        if stop is not None and stop():
            result = ToolResult(tool_name=call.name, success=False, detail=NOT_RUN)
            outcomes.append(ToolOutcome(call, result, tool_message(call.id, NOT_RUN)))
            continue
        error = None
        try:
            args = json.loads(call.arguments)
        except json.JSONDecodeError as e:
            error = f"arguments are not valid JSON ({e}): {call.arguments!r}"
        else:
            if not isinstance(args, dict):
                error = f"arguments must be a JSON object, got {call.arguments!r}"
        if error is not None:
            result = ToolResult(tool_name=call.name, success=False, detail=error)
        else:
            returned = await dispatch(call.name, args)
            result = returned if returned is not None else ToolResult(
                tool_name=call.name, success=True, detail="ok"
            )
        text = result.detail if result.detail else "ok"
        if len(text) > TOOL_MESSAGE_MAX_CHARS and call.name not in UNCUT_TOOLS:
            text = f"{text[:TOOL_MESSAGE_MAX_CHARS]}\n(cut; {len(text)} chars total)"
        outcomes.append(ToolOutcome(call, result, tool_message(call.id, text)))
    return outcomes


async def run_tool_cascade(
    *,
    model: "ModelAdapter",
    system: str,
    messages: list[dict],
    tools: list[type],
    tools_by_name: dict[str, type],
    ctx: "MindCtx",
    sink: "asyncio.Queue[ServerMessage | None] | None",
    max_tokens: int,
    think_budget: int,
    purpose: str,
    max_passes: int = MAX_CASCADE_PASSES,
    on_iter_end: Callable[[int, str, str, list[dict], list[ToolResult], int], None] | None = None,
    check_early_exit: Callable[[int, "MindCtx"], bool] | None = None,
) -> list[dict]:
    """Run an agent tool cascade on ``model``. Mutates `messages` in place
    (``system`` goes in as the first message); also returns it.

    Each pass streams one reply, runs its tool calls in order
    (``run_tool_calls``), and appends the assistant message verbatim plus one
    ``tool`` message per call, so the conversation only grows at the end.
    A pass with tool calls is followed by another; a pass without any ends
    the cascade, as do the 3-strike stuck-tool rule and ``max_passes``.
    ``prior_tokens`` is the previous pass's prompt + completion, so the token
    accounting charges each pass only for what it appended.

    on_iter_end fires after each pass's tools ran — receives (iter_num,
        reasoning, speech, tool_calls, results, duration_ms); tool_calls are
        ``{"id", "name", "arguments"}`` dicts. Caller logs to CascadeLogger here.
    check_early_exit fires after the pass's tools ran and its messages were
        appended. If it returns True, the cascade exits (used by run_agent to
        stop once Report ran, even next to other calls in the same reply).
    """
    schemas = tool_schemas(tools)
    messages.insert(0, {"role": "system", "content": system})
    consecutive_fails: dict[str, int] = {}
    prior_tokens = 0

    async def _dispatch(name: str, arguments: dict) -> ToolResult | None:
        return await dispatch_one(name, arguments, ctx, tools_by_name)

    for iter_num in range(1, max_passes + 1):
        iter_start = time.monotonic()
        think: list[str] = []
        speech: list[str] = []
        calls: tuple[ToolCall, ...] = ()
        end: End | None = None
        stream = model.stream(
            messages=messages,
            tools=schemas,
            max_tokens=max_tokens,
            think_budget=think_budget,
            purpose=purpose,
            on_usage=None,
            prior_tokens=prior_tokens,
        )
        # aclosing() tears the HTTP stream down on any early exit (cancel,
        # exception) — not just on normal exhaustion.
        async with aclosing(stream) as events:
            async for ev in events:
                if isinstance(ev, ThinkDelta):
                    think.append(ev.text)
                elif isinstance(ev, SpeechDelta):
                    speech.append(ev.text)
                elif isinstance(ev, ToolCalls):
                    calls = ev.calls
                elif isinstance(ev, End):
                    end = ev

        usage = end.usage
        if usage.prompt_tokens is not None and usage.completion_tokens is not None:
            prior_tokens = usage.prompt_tokens + usage.completion_tokens
        if end.finish_reason == "length":
            # The adapter already dropped this reply's tool calls.
            logger.warning(
                "%s agent reply hit max_tokens on pass %d (%d tool call(s) dropped)",
                purpose, iter_num, end.dropped_tool_calls,
            )

        outcomes = await run_tool_calls(calls, _dispatch)
        messages.append(end.assistant_message)
        messages.extend(o.message for o in outcomes)
        results = [o.result for o in outcomes]

        duration_ms = int((time.monotonic() - iter_start) * 1000)
        if on_iter_end is not None:
            on_iter_end(
                iter_num,
                "".join(think),
                "".join(speech),
                [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls],
                results,
                duration_ms,
            )

        # Allow caller to exit early (e.g. subagent Report side-effect).
        if check_early_exit is not None and check_early_exit(iter_num, ctx):
            break

        if not outcomes:
            break

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
    else:
        logger.warning(
            "%s cascade still calling tools after %d passes; stopping",
            purpose, max_passes,
        )

    return messages
