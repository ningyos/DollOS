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
    self_profile_max_chars: int = 4000

    # 慢變演化 evidence layer (spec 2026-07-02 §3.2): per-turn provenance,
    # set by MindLoop at drain time; Recall execution upgrades external_ctx
    # mid-cascade. Threaded into PinSelf → self_history.
    current_turn: int = 0
    external_ctx: bool = False
    # Self-directed agenda (spec 2026-07-07 §3.1/§3.3): the `source` values of
    # this turn's REAL memsearch hits — i.e. exactly what actually rendered
    # into [Memory context] this turn (mind_loop._derive_memory_hits, stashed
    # at the same point it's computed, mind_loop.py:414). Read-only for tools:
    # PursueGoal.run reads this to auto-capture `provenance`; it is never
    # populated from tool arguments, so the model cannot write it. Reset to []
    # every turn by MindLoop alongside current_turn/origin_tier — there is no
    # cross-turn bleed.
    turn_memory_sources: list[str] = field(default_factory=list)
    # Per-turn principal (harness spec §6.3, plan 2026-10-27 Task 2):
    # ``dollos.harness.permissions.principal_of`` over this bucket's
    # perceptions — "owner" (live UserSpoke), "owner_dm" (owner AND in a DM),
    # "internal" (no authored input), or "external_public". Recomputed every
    # _run_one_turn alongside origin_tier.
    principal: str = "internal"
    # Taint downgrade (harness spec §6.5 point 2, plan 2026-10-27 Task 6):
    # set by ``ToolExecutor`` once a call that read untrusted content ran
    # (a tool class's ``taints(arguments)``); from then on the turn may only
    # run ``permissions.TAINTED_TOOLS``. Lives here, not on the executor,
    # because MindLoop builds one executor per call and the flag must hold
    # for the rest of the turn, every pass. MindLoop resets it every
    # _run_one_turn alongside principal; an agent's ctx starts False.
    tainted: bool = False
    # The self_profile sections PinSelf may write this turn (remember-
    # corrections plan Task 2, decision 2): set by MindLoop every
    # _run_one_turn alongside principal — reflection → self / relationship /
    # user, any other kind → user / relationship (the self section grows
    # only in reflection). Empty by default, so a ctx nobody set (an agent's)
    # writes no section at all rather than every one.
    pin_sections: frozenset[str] = frozenset()
    # The day's transcript the profile keeper works from (remember-
    # corrections plan Task 4, decision 4): EditOwnerProfile checks each
    # change's quote against the owner lines of THIS file, read from disk,
    # never against text the model supplies. Set only by run_profile_keeper
    # (through run_agent); None everywhere else, and the tool then refuses —
    # no transcript, no edit.
    owner_transcript: Path | None = None
    # Per-turn origin tier (P1e spec §3.4 S1), the compatibility view of
    # ``principal`` (permissions.ORIGIN_TIER): "internal" (owner or
    # internal), "external_dm" (owner in a DM), or "external_public"
    # (anyone else). Memory routing/scoping, the action log, transcripts and
    # speech routing read this; its values did not change with Task 2.
    origin_tier: str = "internal"
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
    # returned a [Self revision candidate] block THIS turn (set by MindLoop, reset at drain
    # alongside the latch). SelfRevision refuses all slot-mutating ops when
    # False — a Mode-B skeptic PASS can flip awaiting_skeptic→awaiting_doll
    # mid-cascade AFTER surfacing already returned None, and the model must not
    # adopt text Doll never saw.
    evolution_candidate_surfaced: bool = False
    evolution_enabled: bool = False
    current_self_min_chars: int = 250
    current_self_max_chars: int = 2000
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
