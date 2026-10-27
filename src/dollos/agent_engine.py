"""run_agent — the reusable per-agent cascade engine.

Relocated from ``SubagentRunner._run_cascade``: one ephemeral worker with a
fresh ``MindState``, the subagent scaffolding system prompt, and a toolkit
that MUST end by calling ``Report``. Returns the Report args dict, or ``None``
if Report was never called (cascade ended naturally / stuck-tool abort).

Reused by ``WorkflowRunner`` (``src/dollos/workflow.py``) for three agent
roles: workflow task-agents, adversarial verify skeptics, and the synthesis
agent (and by the consolidation / evolution keepers). No-nesting is enforced
twice — workers run ``SUB_TOOLS`` (no ``SpawnWorkflow`` schema) AND
``MindCtx.workflow_runner`` is ``None``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from dollos.harness.executor import ToolExecutor
from dollos.harness.runner import AgentThread, PassRecord, Turn, TurnRunner
from dollos.llm.tool_schema import tool_schemas
from dollos.mind.mind_ctx import MindCtx
from dollos.mind.mind_state import MindState
from dollos.mind.sink_resolver import SinkResolver
from dollos.tools import SUB_TOOLS

if TYPE_CHECKING:
    from dollos.harness.executor import ToolResult
    from dollos.harness.turn_policy import TurnKind
    from dollos.llm.model import ModelAdapter
    from dollos.memory import FtsMemory
    from dollos.monitor_runner import MonitorRunner
    from dollos.prompts import PromptRenderer
    from dollos.shell_runner import ShellRunner
    from dollos.tool_outputs import ToolOutputStore


async def run_agent(
    *,
    task: str,
    system: str,
    model: "ModelAdapter",
    renderer: "PromptRenderer",
    memory_root: Path,
    memsearch: "FtsMemory",
    transcripts_root: Path,
    tool_output_store: "ToolOutputStore",
    shell_runner: "ShellRunner | None" = None,
    monitor_runner: "MonitorRunner | None" = None,
    tools: list[type] = SUB_TOOLS,
    kind: "TurnKind",
    max_tokens: int = 4096,
    think_budget: int,
    purpose: str,
    on_iter_end: "Callable[[int, str, str, list[dict], list[ToolResult], int], None] | None" = None,
    owner_transcript: Path | None = None,
    self_profile_max_chars: int = 4000,
) -> dict | None:
    """Run one ephemeral agent cascade. Returns the ``Report`` args dict, or
    ``None`` if Report was never called.

    ``system`` is the already-rendered scaffolding prompt — the caller renders
    it once (from ``renderer``) and reuses it across task / verify / synthesis
    agents. ``renderer`` is accepted for symmetry with the runner's dependency
    set. ``tools`` defaults to ``SUB_TOOLS`` and never includes
    ``SpawnWorkflow``; ``MindCtx(workflow_runner=None)`` is the second
    no-nesting guard. ``on_iter_end`` fires after each pass's tools ran —
    (iter_num from 1, reasoning, speech, tool_calls as ``{"id", "name",
    "arguments"}`` dicts, results, duration_ms) — so worker cascades can log
    to the ``CascadeLogger`` (the live-loop fan-out).
    ``max_tokens`` is the answer budget per call; ``think_budget`` caps the
    thinking on top of it. ``purpose`` labels the calls in ``llm_calls``.
    ``kind`` names the agent's turn kind in its tool-call log records:
    ``worker`` (workflow worker / skeptic / synthesis, ``SUB_TOOLS``) or
    ``keeper`` (consolidation and evolution keepers, the evolution skeptic,
    ``KEEPER_TOOLS``; the profile keeper, ``EditOwnerProfile`` + ``Report``).
    ``owner_transcript`` / ``self_profile_max_chars`` go onto the agent's
    ctx for the profile keeper's ``EditOwnerProfile`` (the transcript its
    quotes are checked against, the profile's shared cap); every other
    agent leaves them at their defaults.

    The agent runs on the ``TurnRunner`` with its kind's policy (at most
    ``MAX_CASCADE_PASSES`` passes, done once Report ran, the stuck-tool
    rule), no sink and no interrupt. It is shown and bound to exactly
    ``tools``; every call goes through a ``ToolExecutor`` for an ``internal``
    turn of ``kind``, which writes its log record. A ``ModelError`` is
    raised.
    """
    tools_by_name: dict[str, type] = {cls.__name__: cls for cls in tools}

    # Fresh MindState — private scratchpad, task as focus, no shared state.
    state = MindState(focus=task)

    ctx = MindCtx(
        mind_state=state,
        memsearch=memsearch,
        memory_root=memory_root,
        transcripts_root=transcripts_root,
        sink_resolver=SinkResolver(),  # agent never speaks to a user sink
        tool_output_store=tool_output_store,
        shell_runner=shell_runner,
        workflow_runner=None,  # no recursion / no nested workflows
        monitor_runner=monitor_runner,
        self_profile_max_chars=self_profile_max_chars,
        owner_transcript=owner_transcript,
        # agent_report starts None; the Report tool sets it to signal exit.
    )

    def _on_pass(p: PassRecord) -> None:
        on_iter_end(
            p.index + 1,
            p.reasoning,
            p.speech,
            [{"id": o.call.id, "name": o.call.name, "arguments": o.call.arguments}
             for o in p.outcomes],
            [o.result for o in p.outcomes],
            int((time.monotonic() - p.started) * 1000),
        )

    executor = ToolExecutor(
        ctx, offered=tools_by_name, bound=tools_by_name,
        principal="internal", kind=kind,
    )
    result = await TurnRunner(model).run(Turn(
        kind=kind,
        principal="internal",
        thread=AgentThread([
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ]),
        tools=tool_schemas(tools),
        execute=executor.run,
        ctx=ctx,
        max_tokens=max_tokens,
        think_budget=think_budget,
        purpose=purpose,
        on_pass=_on_pass if on_iter_end is not None else None,
    ))
    if result.outcome == "brain_down":
        raise result.error

    return ctx.agent_report
