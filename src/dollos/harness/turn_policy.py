"""Turn kinds and their policies (harness spec §9 S3, mind-loop spec §4.3,
plan 2026-10-27 Task 3).

``kind_of`` names what a batch of perceptions is (a conversation, a
reflection, a diary / agenda / pulse moment, or safe mode); ``POLICIES``
says what a turn of that kind does: whether it speaks, which tools its kind
allows, which prompt blocks it adds. A turn's tools =
``POLICIES[kind]`` ∩ ``TOOL_MATRIX[principal]`` (+ the kind's extras),
computed by ``bound_tools``; the fixed list the model is shown is
``offered_tools``.

Safe mode is a kind for TOOLS only: it can switch on in the middle of a turn
(after the failure threshold) and then narrows the next pass's binding, but
the turn keeps speaking, writing its transcript and rendering its blocks as
the kind of its batch (``moment_of``). So MindLoop keeps the batch's kind and
asks ``effective_kind`` for the binding.

The two kinds that are not MindLoop turns, ``worker`` (a workflow worker /
skeptic / synthesis agent) and ``keeper`` (consolidation and evolution
keepers, the evolution skeptic), are defined here from ``SUB_TOOLS`` /
``KEEPER_TOOLS``; ``agent_engine.run_agent`` runs them on the Turn Runner.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from dollos.harness.permissions import (
    ORIGIN_TIER,
    TOOL_MATRIX,
    TRUST_ORDER,
    Principal,
    tools_for,
)
from dollos.tools import KEEPER_TOOLS, SUB_TOOLS

if TYPE_CHECKING:
    from dollos.mind.mind_state import Perception

TurnKind = Literal[
    "conversation", "reflection", "diary", "agenda", "pulse", "safe_mode",
    "worker", "keeper",
]

# Config switches an extra tool can depend on, resolved at bind time.
Condition = Literal["self_profile", "evolution"]

# Budget cap on a MindLoop turn's tool loop (spec §8.2): a turn runs at most
# this many model calls. Generous enough for legitimate multi-step plans, but
# a rotating-invalid-call storm or a tool loop terminates deterministically.
# This is a convergence criterion, NOT a fallback.
MAX_SYNC_REFEED_PASSES = 8

# An agent that keeps calling tools without ever ending stops here.
MAX_CASCADE_PASSES = 16


@dataclass(frozen=True)
class Extra:
    """A tool a kind adds on top of the registry (it is never a member of
    MindLoop's tool registry itself — added explicitly, not filtered in, or it
    would silently never appear). ``when`` names the config switch it needs.
    It is bound only where ``TOOL_MATRIX`` allows the principal."""
    tool: str
    when: Condition | None = None


@dataclass(frozen=True)
class TurnPolicy:
    # Naked-text speech goes out to the sink (_emit_sentence).
    speaks: bool
    # The registry tools this kind may run, or "all" for the whole registry.
    kind_tools: frozenset[str] | Literal["all"]
    extra_tools: tuple[Extra, ...] = ()
    # Prompt blocks this kind adds: "tool_outcomes" ([Tool outcomes]),
    # "evolution_candidate" (the slow-evolution candidate surfacing),
    # "body_signal" ([Body signal]), "today_log" (a one-off diary request
    # carrying [Today's log]: never stored, private conversation and its
    # [Self pulse]/[Cognition] untouched).
    extra_blocks: frozenset[str] = frozenset()
    # The turn's speech is written to the day's doll transcript.
    doll_transcript: bool = True
    # The Turn Runner (harness/runner.py): at most this many passes; the turn
    # is done once Report ran (an agent); and which tool-failure rule ends it
    # — "safe_mode" (MindLoop: same tool 3x in a row, or
    # SAFE_MODE_FAIL_THRESHOLD consecutive failures; she enters safe mode)
    # or "stuck_tool" (an agent: one tool's failures reach 3, counted across
    # other tools' failures).
    max_passes: int = MAX_SYNC_REFEED_PASSES
    must_report: bool = False
    failure_rule: Literal["safe_mode", "stuck_tool"] = "safe_mode"
    # MCP tools (harness spec §5, plan 2026-10-27 Task 7) are bound on this
    # kind, each where its server's ``min_principal`` row allows the
    # principal. Only a conversation: a self-wake (diary, agenda, pulse,
    # reflection), safe mode or an agent never reaches an outside server.
    mcp_tools: bool = False


_REFLECTION_EXTRAS = (
    # NoteToolLesson: reflection turns only (Spec B §5).
    Extra("NoteToolLesson"),
    # PinSelf: A1 self-profile (spec Task 5), when self_profile_enabled.
    # Owner / internal only (TOOL_MATRIX): external_public is never offered
    # PinSelf, so its reflection turn does not bind it either.
    Extra("PinSelf", when="self_profile"),
    # SelfRevision: 慢變演化 (spec §3.4), when evolution_enabled — the
    # registry is authoritative, so no separate EVOLUTION_TOOLS list is kept.
    Extra("SelfRevision", when="evolution"),
    # LearnName: see below.
    Extra("LearnName"),
)

POLICIES: dict[TurnKind, TurnPolicy] = {
    # Every batch that is not one of the moments below: the owner talking
    # (local chat/voice, or a DM), a stranger, or an event (tool result,
    # monitor, schedule, ...). The principal decides the tools.
    #
    # LearnName (Part A / A2, self-learned-aliases spec §3.3 — R1-hardened
    # SECURITY BOUNDARY): bound ONLY on live, owner-present turns — principal
    # owner (a live UserSpoke = owner at the keyboard) or owner_dm (the
    # ChannelMessage from the owner IS the live first-hand utterance, no
    # additional signal needed). It is NEVER bound on ``external_public``
    # (any stranger, including the owner posting in a public channel) or on
    # a pure-reflection turn (``ReflectionMoment`` with no ``UserSpoke`` —
    # the exact whitewash path the R1 review's C1 finding killed: reflection
    # turns are always ``origin_tier == "internal"`` regardless of who was
    # actually heard, so gating on origin_tier alone would let a stranger's
    # earlier utterance get written as an owner-taught alias after the fact).
    # Trust here is enforced by REGISTRY AVAILABILITY, not by a post-hoc read
    # of ``ctx.origin_tier`` inside the tool — a stranger's turn never has
    # LearnName in its dict, full stop (TOOL_MATRIX row: owner, owner_dm).
    "conversation": TurnPolicy(
        speaks=True, kind_tools="all", extra_tools=(Extra("LearnName"),),
        mcp_tools=True,
    ),
    # Reflection turn → full registry + NoteToolLesson, + PinSelf when
    # self_profile_enabled, + SelfRevision when evolution_enabled, +
    # LearnName when the owner is present. The external_public reduction WINS
    # OVER reflection expansion (P1e S5): an external_public turn that is also
    # a ReflectionMoment never gets SelfRevision, NoteToolLesson, or any
    # registry tool outside the external_public row — a stranger must never
    # reach Shell / SpawnWorkflow / SpawnMonitor / RemoveMonitor /
    # InvokeSkill / WriteSchedule / SelfRevision.
    "reflection": TurnPolicy(
        speaks=True, kind_tools="all", extra_tools=_REFLECTION_EXTRAS,
        extra_blocks=frozenset({"tool_outcomes", "evolution_candidate"}),
    ),
    # Dedicated diary turn (spec 2026-07-07 §2.3, Task 7, LOAD-BEARING): the
    # ONLY tools available on a pure DiaryMoment turn. WriteDiary is the
    # point of the turn; Recall lets her look up earlier-in-the-day context
    # if the injected [Today's log] isn't enough. Everything else (Shell/
    # SpawnWorkflow/NoteMemory/etc.) is excluded so the turn cannot do
    # anything BUT write (or research toward) the diary entry. A batch that
    # co-bundles a DiaryMoment with a real UserSpoke is not a diary turn
    # (kind_of) and runs as a normal, non-narrowed turn instead, same as the
    # agenda case.
    # Speech is suppressed (_emit_sentence): a pure DiaryMoment turn is
    # private journaling, not a conversation, so any naked text she streams
    # (e.g. commentary alongside the WriteDiary call) must not leak to a
    # connected sink. Her doll transcript is suppressed too: her private
    # journaling commentary (if any leaked past _emit_sentence's own
    # suppression into _turn_speech — it still gets buffered there for
    # trace/audit) must not also land in the day's action-log transcript that
    # the NEXT diary turn reads back via _read_today_log.
    "diary": TurnPolicy(
        speaks=False, kind_tools=frozenset({"WriteDiary", "Recall"}),
        extra_blocks=frozenset({"today_log"}), doll_transcript=False,
    ),
    # Self-directed agenda (spec 2026-07-07 §5.1, §5.2, R1-C1, SECURITY-
    # LOAD-BEARING): v1 tool scope for a pure AgendaMoment turn (no user
    # present). Deliberately excludes: Shell/SpawnWorkflow/SpawnMonitor/
    # WriteSchedule (no autonomous external action), SelfRevision/PinSelf (no
    # autonomous core-self edits), NoteMemory (R1-I1 — blocks a self-directed
    # turn from bootstrapping its own "grounding" for a future PursueGoal),
    # and PursueGoal itself (R1-I3 — genesis only happens on reflection/
    # reactive turns; letting an autonomous turn open new agenda items would
    # make the agenda gate self-perpetuating). MoodTool stays in (v1
    # decision, spec §11.6 — her mood can drift on its own while she's
    # alone). A batch that co-bundles an AgendaMoment with a real UserSpoke
    # is not an agenda turn (kind_of) — full registry, the user's request is
    # never silently restricted. Speech is suppressed.
    "agenda": TurnPolicy(
        speaks=False,
        kind_tools=frozenset({"Recall", "AdvanceGoal", "CloseLoop", "MoodTool"}),
    ),
    # Pure PulseMoment turn (spec 2026-07-09 §5.3): cognition-only + speech
    # ON. She can Recall/NoteMemory/adjust Mood and SPEAK UP, but no
    # autonomous external action (no Shell/Workflow) on a self-wake. Speech
    # is NOT gated by this set — it stays on because ``speaks`` is True here
    # (unlike agenda/diary). A co-batched UserSpoke makes it a normal turn.
    "pulse": TurnPolicy(
        speaks=True, kind_tools=frozenset({"Recall", "NoteMemory", "MoodTool"}),
        extra_blocks=frozenset({"body_signal"}),
    ),
    # Read-only safe mode (spec §8.3): the only tools available while
    # safe_mode is set: read-only / reversible reads plus naked-text speech
    # (always available, not a tool). Everything that writes memory/diary/
    # schedule/scratchpad or spawns an external action is excluded so a
    # failure storm cannot do irreversible damage while the loop is wedged.
    # Safe mode has TOP priority (wins over every other kind, on every tier).
    # ReadToolOutput / GrepToolOutput are not in external_public's row, so a
    # stranger's turn in safe mode binds Recall only.
    # Only the tools: speech and blocks follow the batch's kind (module doc).
    "safe_mode": TurnPolicy(
        speaks=True,
        kind_tools=frozenset({"Recall", "ReadToolOutput", "GrepToolOutput"}),
    ),
    # Workflow worker / skeptic / synthesis agent: no sink, ends with Report.
    "worker": TurnPolicy(
        speaks=False, kind_tools=frozenset(c.__name__ for c in SUB_TOOLS),
        max_passes=MAX_CASCADE_PASSES, must_report=True, failure_rule="stuck_tool",
    ),
    # B2 memory-keeper allowlist: only Report + Scratchpad (no Shell/
    # NoteMemory/SpawnMonitor/RemoveMonitor).
    "keeper": TurnPolicy(
        speaks=False, kind_tools=frozenset(c.__name__ for c in KEEPER_TOOLS),
        max_passes=MAX_CASCADE_PASSES, must_report=True, failure_rule="stuck_tool",
    ),
}


def _pure(perceptions: list["Perception"], kind: str) -> bool:
    return bool(perceptions) and all(p.kind == kind for p in perceptions)


def moment_of(perceptions: Iterable["Perception"]) -> TurnKind:
    """The kind of this batch, without safe mode.

    Reflection: ANY ReflectionMoment in the batch (a batch can legitimately
    contain BOTH a ReflectionMoment and a UserSpoke — both are origin-less
    and land in the same internal bucket, see test_mind_loop.py's MF-2; the
    principal, not the kind, then says the owner is present).

    Diary / agenda / pulse: only a PURE batch — the ENTIRE batch is that
    moment, not merely "no UserSpoke" (self-directed agenda spec 2026-07-07
    §5.2, R1-C1, hardened by the whole-branch review's Finding 1).
    ``drain_grouped`` (perception_queue.py) batches ALL origin-less internal
    perceptions into ONE bucket — UserSpoke, MonitorFired,
    ToolResultArrived, ScheduledMoment, Awoke, BridgeDown, McpDown,
    SafeModeEntered, etc. If ANY other perception rides along with an
    AgendaMoment, this is NOT a pure-agenda turn, full stop: a "no
    UserSpoke" check alone would still whitewash a co-batched MonitorFired/
    ToolResultArrived/ScheduledMoment into the agenda tools and silently
    swallow its speech (an operational regression, not a security widening
    — the restriction only ever narrows availability). Getting this wrong
    for UserSpoke specifically would restrict a REAL user request to the
    agenda tools (no Shell/Workflow) — the exact whitewash shape the
    LearnName-C1 finding killed; this generalizes that same guard to every
    other origin-less internal perception kind too. The same pure-batch
    shape holds for DiaryMoment (spec §2.3, Task 7) and PulseMoment (spec
    2026-07-09 §5.2).
    """
    perceptions = list(perceptions)
    if any(p.kind == "ReflectionMoment" for p in perceptions):
        return "reflection"
    if _pure(perceptions, "DiaryMoment"):
        return "diary"
    if _pure(perceptions, "AgendaMoment"):
        return "agenda"
    if _pure(perceptions, "PulseMoment"):
        return "pulse"
    return "conversation"


def effective_kind(moment: TurnKind, *, safe_mode: bool) -> TurnKind:
    """Safe mode has top priority over the batch's kind."""
    return "safe_mode" if safe_mode else moment


def kind_of(perceptions: Iterable["Perception"], *, safe_mode: bool) -> TurnKind:
    """This batch's turn kind (safe mode first, then ``moment_of``)."""
    return effective_kind(moment_of(perceptions), safe_mode=safe_mode)


def _holds(extra: Extra, *, self_profile: bool, evolution: bool) -> bool:
    if extra.when == "self_profile":
        return self_profile
    if extra.when == "evolution":
        return evolution
    return True


def bound_tools(
    kind: TurnKind, principal: Principal, registry: Iterable[str], *,
    self_profile: bool, evolution: bool,
    mcp: Mapping[str, frozenset[Principal]] = {},
) -> frozenset[str]:
    """What a ``kind`` turn of ``principal`` is bound to: the kind's registry
    tools ∩ the principal's TOOL_MATRIX row, + the kind's extras, + on a
    ``mcp_tools`` kind the MCP tools whose row (``mcp``: name → the
    principals its server's ``min_principal`` allows) has the principal."""
    policy = POLICIES[kind]
    names = frozenset(registry)
    if policy.kind_tools != "all":
        names &= policy.kind_tools
    names &= tools_for(principal)
    extras = {
        e.tool for e in policy.extra_tools
        if _holds(e, self_profile=self_profile, evolution=evolution)
        and principal in TOOL_MATRIX[e.tool]
    }
    remote = {
        n for n, row in mcp.items() if policy.mcp_tools and principal in row
    }
    return names | extras | remote


def offered_tools(
    principal: Principal, registry: Iterable[str], *,
    self_profile: bool, evolution: bool,
    mcp: Mapping[str, frozenset[Principal]] = {},
) -> frozenset[str]:
    """The fixed tool list a principal's turns are shown (plan 2026-10-18
    decision 3): one list per origin tier, so the registry plus every extra
    whose config switch is on, kept where TOOL_MATRIX allows any principal of
    the same origin tier. Owner and internal turns share the private
    conversation (origin_tier "internal"), so its list is the union of both
    rows — the owner's LearnName included. MCP tools (``mcp``: name → row)
    join the same way, by their own row; the list is fixed for the run
    because the MCP tools are listed once at boot (plan Task 7)."""
    tier = ORIGIN_TIER[principal]
    same_tier = {q for q in TRUST_ORDER if ORIGIN_TIER[q] == tier}
    candidates = set(registry) | {
        e.tool for policy in POLICIES.values() for e in policy.extra_tools
        if _holds(e, self_profile=self_profile, evolution=evolution)
    }
    builtin = {n for n in candidates if same_tier & TOOL_MATRIX[n]}
    return frozenset(builtin | {n for n, row in mcp.items() if same_tier & row})
