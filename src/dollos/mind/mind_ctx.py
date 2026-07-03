"""MindCtx — replaces ToolCtx. Tools mutate mind_state directly; sink
resolved at emit time via sink_resolver()."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dollos.character import Enforcement
    from dollos.ipc.channel_registry import ChannelRegistry
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

    # 慢變演化 evidence layer (spec 2026-07-02 §3.2): per-turn provenance,
    # set by MindLoop at drain time; Recall execution upgrades external_ctx
    # mid-cascade. Threaded into PinSelf → self_history.
    current_turn: int = 0
    external_ctx: bool = False
    # Per-bucket origin channel_id (spec §3.1 R2-C1): set by MindLoop.iterate
    # before dispatching each drain_grouped() bucket to _run_one_turn, so
    # sink_resolver(origin) routes this turn's output to the matching
    # external sink instead of stealing another channel's / the internal
    # sink's stream. None for origin-less (internal) buckets.
    current_origin: str | None = None
    # Daemon-wide channel_id → locus/kind registry (spec §3.1 Task 1/5):
    # MindLoop consults locus_of(current_origin) to decide whether a
    # streamed sentence goes out as AddressedText (external) or TextChunk
    # (internal/unregistered). None in contexts that never see external
    # origins (e.g. worker-agent ctx, most existing tests).
    channel_registry: "ChannelRegistry | None" = None

    # 慢變演化 (spec 2026-07-02 §3.4): per-turn SelfRevision latch (reset at
    # drain by MindLoop) + static-per-run evolution config + pack enforcement.
    evolution_latched: bool = False
    # Surfaced-this-turn gate (review F5): True only when surface_or_expire
    # returned a [人格演化候選] block THIS turn (set by MindLoop, reset at drain
    # alongside the latch). SelfRevision refuses all slot-mutating ops when
    # False — a Mode-B skeptic PASS can flip awaiting_skeptic→awaiting_doll
    # mid-cascade AFTER surfacing already returned None, and the model must not
    # adopt text Doll never saw.
    evolution_candidate_surfaced: bool = False
    evolution_enabled: bool = False
    current_self_min_chars: int = 80
    current_self_max_chars: int = 600
    enforcement: "Enforcement | None" = None
    # 慢變演化 Mode-A decision-event bookkeeping (spec §3.3, Plan 3): SelfRevision
    # reads these to reset (adopt) / double-with-cap (reject) the keeper
    # interval — mirrors EvolutionTrigger's own knobs so the tool and the
    # trigger agree on base/cap without a second source of truth.
    evolution_base_interval_days: float = 7.0
    evolution_max_interval_days: float = 28.0

    # Worker-agent-only: Report tool stashes its result here; None in main cascade.
    agent_report: dict | None = field(default=None)

    # Cascade compat: always None; cascade.py gates ErrorMsg on `ctx.sink is not None`.
    sink: None = field(default=None, init=False, repr=False)
