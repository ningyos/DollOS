"""MindCtx — replaces ToolCtx. Tools mutate mind_state directly; sink
resolved at emit time via sink_resolver()."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dollos.memory import FtsMemory

    from dollos.mind.mind_state import MindState
    from dollos.mind.sink_resolver import SinkResolver
    from dollos.monitor_runner import MonitorRunner
    from dollos.shell_runner import ShellRunner
    from dollos.tool_outputs import ToolOutputStore
    from dollos.workflow import WorkflowRunner


@dataclass
class MindCtx:
    """Execution context for MindLoop tools.

    Tools mutate mind_state directly (no separate scratchpad/sink field).
    Sink is resolved at emit time via sink_resolver().

    agent_report: set by the Report tool inside a worker-agent cascade;
        run_agent reads it back so WorkflowRunner can build the
        ToolResultArrived perception. None for Doll's main cascade (Report is
        not in MAIN_TOOLS) and None inside any worker agent until it Reports.

    workflow_runner: the WorkflowRunner Doll dispatches background workflows to;
        None inside a worker agent's ctx (the no-nesting guard — a worker
        cannot spawn further workflows).

    sink: always None on MindCtx. cascade.py checks `ctx.sink is not None` to
        decide whether to push ErrorMsg to a user-facing sink; workers have no
        live user sink, so this is always None here.
    """
    mind_state: "MindState"
    memsearch: "FtsMemory"
    memory_root: Path
    transcripts_root: Path
    sink_resolver: "SinkResolver"
    tool_output_store: "ToolOutputStore"
    shell_runner: "ShellRunner"
    workflow_runner: "WorkflowRunner | None"
    monitor_runner: "MonitorRunner"

    # A1 self-profile — total-char cap for self_profile.md (from Settings).
    self_profile_max_chars: int = 1200

    # Worker-agent-only: Report tool stashes its result here; None in main cascade.
    agent_report: dict | None = field(default=None)

    # Cascade compat: always None; cascade.py gates ErrorMsg on `ctx.sink is not None`.
    sink: None = field(default=None, init=False, repr=False)
