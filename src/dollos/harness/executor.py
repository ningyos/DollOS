"""ToolExecutor: runs one tool call against this turn's binding (harness
spec §6.4, plan 2026-10-27 Task 4 / S5).

The model is shown the turn's ``offered`` tools; only its ``bound`` tools may
run. ``ToolExecutor.run`` has three paths:

- a name outside ``offered`` → unknown tool, a failure (decision ``deny``);
- offered but not bound → "not available in this turn", NOT a failure
  (``unbound=True``; decision ``unbound``), nothing runs;
- bound → pydantic validation, then the tool runs (decision ``allow``).

Each call writes one ``tool_call`` record to the cascade JSONL log
(``logging_config.configure_cascade_logging``): principal, kind, tool, a
short argument summary, the decision, and whether the turn was tainted.

Taint downgrade (spec §6.5 point 2, plan Task 6): once an allowed call that
read untrusted content has run (its tool class's ``taints(arguments)`` is
true), the rest of the turn may only run ``permissions.TAINTED_TOOLS``. The
flag lives on ``ctx.tainted`` (MindLoop resets it every turn; an agent's ctx
is fresh), so it holds across the per-call executors MindLoop builds and
across passes. The offered list does not change (plan decision 4: the tool
JSON stays byte-identical for the server cache); a tool that was bound but
no longer is gets the same "not available in this turn" refusal as any
unbound one. An external_public turn is never tainted: its row is already
the floor, and tainting would strip NoteMemory from strangers.

``dispatch_one`` (validate + run one call against a registry) is shared by
the MindLoop turn and the agents (``agent_engine.run_agent``).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import ValidationError

from dollos.harness.permissions import TAINTED_TOOLS

if TYPE_CHECKING:
    from dollos.harness.permissions import Principal
    from dollos.harness.turn_policy import TurnKind
    from dollos.mind.mind_ctx import MindCtx

logger = logging.getLogger(__name__)
_log = structlog.get_logger("cascade")

Decision = Literal["allow", "deny", "unbound"]

# The argument summary in the log record is cut here; a Shell command is cut
# much sooner, so the log never holds a whole command line (it can carry a
# token or a password).
ARGS_SUMMARY_MAX_CHARS = 200
SHELL_COMMAND_MAX_CHARS = 40


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


def format_unknown_tool(name: str, registry: Mapping[str, type]) -> str:
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
    registry: Mapping[str, type],
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


def turn_label(
    kind: "TurnKind", principal: "Principal", *, tainted: bool = False,
) -> str:
    """The turn as named in the refusal of an offered-but-unbound tool:
    a tainted turn, then safe mode, then a stranger's turn, then the kind (a
    conversation is "owner" when the owner is present, else "normal")."""
    if tainted:
        return "after reading external content"
    if kind == "safe_mode":
        return "safe mode"
    if principal == "external_public":
        return "external_public"
    if kind == "conversation":
        return "owner" if principal in ("owner", "owner_dm") else "normal"
    return kind


def summarize_arguments(name: str, arguments: dict) -> str:
    """A short JSON summary of a call's arguments for the log record: cut at
    ARGS_SUMMARY_MAX_CHARS, with a Shell command cut at
    SHELL_COMMAND_MAX_CHARS first."""
    command = arguments.get("command")
    if name == "Shell" and isinstance(command, str):
        cut = len(command) - SHELL_COMMAND_MAX_CHARS
        if cut > 0:
            arguments = {
                **arguments,
                "command": f"{command[:SHELL_COMMAND_MAX_CHARS]}…(+{cut} chars)",
            }
    text = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) > ARGS_SUMMARY_MAX_CHARS:
        text = f"{text[:ARGS_SUMMARY_MAX_CHARS]}…"
    return text


Dispatch = Callable[[str, dict, Mapping[str, type]], Awaitable[ToolResult | None]]


class ToolExecutor:
    """One turn's tool calls: ``offered`` is what the model was shown,
    ``bound`` what this turn may run (``bound`` ⊆ ``offered``).

    ``dispatch(name, arguments, bound)`` validates and runs a call; it gets
    every call that is not an offered-but-unbound one, so an unknown name is
    worded by ``dispatch_one`` (``format_unknown_tool`` against ``bound``),
    as before. It defaults to ``dispatch_one`` on ``ctx``; MindLoop passes
    its own wrapper, which adds the side effects only a live turn has (tool
    memory, the action log, the diary-written flag). The agents use the
    default, so they never get those."""

    def __init__(
        self,
        ctx: "MindCtx",
        *,
        offered: Mapping[str, type],
        bound: Mapping[str, type],
        principal: "Principal",
        kind: "TurnKind",
        dispatch: Dispatch | None = None,
    ) -> None:
        self._ctx = ctx
        self._offered = offered
        self._bound = bound
        self._principal = principal
        self._kind = kind
        self._dispatch = dispatch

    def _tainted(self) -> bool:
        # external_public is never tainted (module docstring); the check is
        # here too so a flag set some other way cannot narrow its row.
        return self._ctx.tainted and self._principal != "external_public"

    def _effective_bound(self) -> Mapping[str, type]:
        """``bound``, narrowed to ``TAINTED_TOOLS`` once the turn is tainted."""
        if not self._tainted():
            return self._bound
        return {n: c for n, c in self._bound.items() if n in TAINTED_TOOLS}

    def decide(self, name: str) -> Decision:
        if name not in self._offered:
            return "deny"
        if name not in self._effective_bound():
            return "unbound"
        return "allow"

    async def run(self, name: str, arguments: dict) -> ToolResult | None:
        decision = self.decide(name)
        tainted = self._tainted()
        self._log(name, arguments, decision, tainted)
        bound = self._effective_bound()
        if decision == "unbound":
            available = ", ".join(sorted(bound)) or "(none)"
            label = turn_label(self._kind, self._principal, tainted=tainted)
            return ToolResult(
                tool_name=name,
                success=False,
                detail=(
                    f"{name} is not available in this turn ({label}); "
                    f"available now: {available}"
                ),
                unbound=True,
            )
        if self._dispatch is not None:
            result = await self._dispatch(name, arguments, bound)
        else:
            result = await dispatch_one(name, arguments, self._ctx, bound)
        if decision == "allow" and (result is None or result.success):
            self._maybe_taint(name, arguments)
        return result

    def _maybe_taint(self, name: str, arguments: dict) -> None:
        """Taint the turn after a clean call that read untrusted content.
        Most tools define no ``taints``; Recall's is ``include_external``;
        Task 7's MCP tools with ``untrusted_output`` will return True."""
        if self._principal == "external_public" or self._ctx.tainted:
            return
        taints = getattr(self._bound[name], "taints", None)
        if taints is None or not taints(arguments):
            return
        self._ctx.tainted = True
        try:
            _log.info(
                "turn_tainted",
                principal=self._principal,
                kind=self._kind,
                tool=name,
            )
        except Exception:
            logger.exception("turn_tainted log failed; continuing")

    def _log(
        self, name: str, arguments: dict, decision: Decision, tainted: bool,
    ) -> None:
        try:
            _log.info(
                "tool_call",
                principal=self._principal,
                kind=self._kind,
                tool=name,
                args=summarize_arguments(name, arguments),
                decision=decision,
                tainted=tainted,
            )
        except Exception:
            logger.exception("tool_call log failed; continuing")
