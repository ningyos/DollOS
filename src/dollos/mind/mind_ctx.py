"""MindCtx — replaces ToolCtx. Tools mutate mind_state directly; sink
resolved at emit time via sink_resolver()."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memsearch import MemSearch

    from dollos.mind.mind_state import MindState
    from dollos.mind.sink_resolver import SinkResolver
    from dollos.monitor_runner import MonitorRunner
    from dollos.shell_runner import ShellRunner
    from dollos.subagent import SubagentRunner
    from dollos.tool_outputs import ToolOutputStore


@dataclass
class MindCtx:
    """Execution context for MindLoop tools.

    Tools mutate mind_state directly (no separate scratchpad/sink field).
    Sink is resolved at emit time via sink_resolver().

    subagent_report: set by Report tool inside a subagent cascade; SubagentRunner
        reads it back to build the ToolResultArrived perception. None for Doll's
        main cascade (Report is not in MAIN_TOOLS).

    sink: always None on MindCtx. cascade.py checks `ctx.sink is not None` to
        decide whether to push ErrorMsg to a user-facing sink; subagents have no
        live user sink, so this is always None here.
    """
    mind_state: "MindState"
    memsearch: "MemSearch"
    memory_root: Path
    transcripts_root: Path
    sink_resolver: "SinkResolver"
    tool_output_store: "ToolOutputStore"
    shell_runner: "ShellRunner"
    subagent_runner: "SubagentRunner"
    monitor_runner: "MonitorRunner"

    # Subagent-only: Report tool stashes its result here; None in main cascade.
    subagent_report: dict | None = field(default=None)

    # Cascade compat: always None; cascade.py gates ErrorMsg on `ctx.sink is not None`.
    sink: None = field(default=None, init=False, repr=False)
