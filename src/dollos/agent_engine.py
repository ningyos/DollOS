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

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from dollos.cascade import run_tool_cascade
from dollos.mind.mind_ctx import MindCtx
from dollos.mind.mind_state import MindState
from dollos.mind.sink_resolver import SinkResolver
from dollos.tools import SUB_TOOLS

if TYPE_CHECKING:
    from dollos.cascade.tool_loop import ToolResult
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
    max_tokens: int = 4096,
    think_budget: int,
    purpose: str,
    on_iter_end: "Callable[[int, str, str, list[dict], list[ToolResult], int], None] | None" = None,
) -> dict | None:
    """Run one ephemeral agent cascade. Returns the ``Report`` args dict, or
    ``None`` if Report was never called.

    ``system`` is the already-rendered scaffolding prompt — the caller renders
    it once (from ``renderer``) and reuses it across task / verify / synthesis
    agents. ``renderer`` is accepted for symmetry with the runner's dependency
    set. ``tools`` defaults to ``SUB_TOOLS`` and never includes
    ``SpawnWorkflow``; ``MindCtx(workflow_runner=None)`` is the second
    no-nesting guard. ``on_iter_end`` is threaded into ``run_tool_cascade`` so
    worker cascades can log to the ``CascadeLogger`` (the live-loop fan-out).
    ``max_tokens`` is the answer budget per call; ``think_budget`` caps the
    thinking on top of it. ``purpose`` labels the calls in ``llm_calls``.
    """
    tools_by_name: dict[str, type] = {cls.__name__: cls for cls in tools}
    messages: list[dict] = [{"role": "user", "content": task}]

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
        # agent_report starts None; the Report tool sets it to signal exit.
    )

    def _check_early_exit(iter_num: int, c: MindCtx) -> bool:
        # Report stashes its args in ctx.agent_report; once it ran (alone or
        # next to other calls in the same reply), the agent is done.
        return c.agent_report is not None

    await run_tool_cascade(
        model=model,
        system=system,
        messages=messages,
        tools=tools,
        tools_by_name=tools_by_name,
        ctx=ctx,
        sink=None,
        max_tokens=max_tokens,
        think_budget=think_budget,
        purpose=purpose,
        on_iter_end=on_iter_end,
        check_early_exit=_check_early_exit,
    )

    return ctx.agent_report
