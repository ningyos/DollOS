"""Principal of a turn and the tool permission table (harness spec §6.3,
plan 2026-10-27 Task 2 / S2).

``principal_of`` names who a turn acts for; ``TOOL_MATRIX`` says which tools
each principal may EVER be bound to. Turn-kind narrowing (diary, agenda,
pulse, reflection, safe mode) is not in this table — that is TurnPolicy
(``dollos.harness.turn_policy``). A turn's tools = its kind's tools ∩ its
principal's row.

Owner DM = owner (user decision 2026-10-27): the owner in a Discord DM gets
the same row as the owner at the local keyboard, Shell included. The risk
that a compromised Discord account equals the owner on the home computer
was accepted by the user on that date. Strangers, non-owner DMs (MCP peers
included), and the owner posting in a PUBLIC channel stay
``external_public``. The two owner values stay separate because the reply
goes elsewhere (Discord vs local) and the memory/transcript scoping keyed
on ``origin_tier`` differs.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from dollos.mind.mind_state import Perception

Principal = Literal["owner", "owner_dm", "internal", "external_public"]

# Trust order, highest first. A turn's principal is the LOWEST-trust input in
# its batch (spec §6.5). Only perceptions with an author count as inputs: a
# UserSpoke (owner at the keyboard / voice) and a ChannelMessage (owner_dm or
# external_public). Doll's own perceptions (ReflectionMoment, DiaryMoment,
# AgendaMoment, PulseMoment, ToolResultArrived, ScheduledMoment, ...) carry
# no author, so they never lower a batch; a batch with no authored input is
# ``internal``. That keeps a ReflectionMoment co-batched with a live
# UserSpoke an owner turn (test_mind_loop.py MF-2, LearnName gate).
# owner_dm sits below owner: it arrives over a remote, account-authenticated
# channel rather than the local machine, and in an owner-DM + UserSpoke batch
# the ChannelMessage side keeps deciding origin_tier, exactly as before.
TRUST_ORDER: tuple[Principal, ...] = ("owner", "owner_dm", "internal", "external_public")
_RANK: dict[Principal, int] = {p: i for i, p in enumerate(TRUST_ORDER)}

# Compatibility ``ctx.origin_tier`` per principal: memory routing/scoping,
# action log, transcripts and speech routing still read origin_tier, and its
# values do not change.
ORIGIN_TIER: dict[Principal, str] = {
    "owner": "internal",
    "owner_dm": "external_dm",
    "internal": "internal",
    "external_public": "external_public",
}


def _author_principal(p: "Perception") -> Principal | None:
    """The principal of one perception's author, or None for Doll's own.

    Only ``author_is_owner`` AND ``is_dm`` is owner_dm; a missing or falsy
    field fails CLOSED to external_public (whole-branch review C1: the owner
    in a public channel is answered in public)."""
    if p.kind == "ChannelMessage":
        data = p.data or {}
        if data.get("author_is_owner") and data.get("is_dm"):
            return "owner_dm"
        return "external_public"
    if p.kind == "UserSpoke":
        return "owner"
    return None


def principal_of(perceptions: Iterable["Perception"]) -> Principal:
    """This batch's principal: the lowest-trust authored input, else internal."""
    authored = [a for a in map(_author_principal, perceptions) if a is not None]
    if not authored:
        return "internal"
    return max(authored, key=_RANK.__getitem__)


_ALL: frozenset[Principal] = frozenset(TRUST_ORDER)
_OWNER: frozenset[Principal] = frozenset({"owner", "owner_dm"})
_TRUSTED: frozenset[Principal] = frozenset({"owner", "owner_dm", "internal"})

# One row per tool: the principals that may ever run it. Copied from
# MindLoop._active_tool_registry / _offered_tools, with owner_dm = owner.
# - Recall / NoteMemory / WriteDiary: every principal (the external_public
#   row); memory scoping by origin_tier happens inside the tools.
# - PinSelf: owner / internal (reflection turns only, turn_policy).
# - LearnName: the owner only — never internal (a pure reflection turn must
#   not write a stranger's earlier words as an owner-taught alias, R1 C1),
#   never external_public.
# - ReadToolOutput / GrepToolOutput: never external_public, safe mode
#   included.
# - Report: bound only by agent turns (workflow worker, keepers), which are
#   spawned from owner / internal turns; no MindLoop turn kind includes it.
TOOL_MATRIX: dict[str, frozenset[Principal]] = {
    "Recall": _ALL,
    "NoteMemory": _ALL,
    "WriteDiary": _ALL,
    "PinSelf": _TRUSTED,
    "LearnName": _OWNER,
    "WriteSchedule": _TRUSTED,
    "Shell": _TRUSTED,
    "InvokeSkill": _TRUSTED,
    "SpawnWorkflow": _TRUSTED,
    "SpawnMonitor": _TRUSTED,
    "RemoveMonitor": _TRUSTED,
    "ReadToolOutput": _TRUSTED,
    "GrepToolOutput": _TRUSTED,
    "Scratchpad": _TRUSTED,
    "SetFocus": _TRUSTED,
    "OpenLoop": _TRUSTED,
    "CloseLoop": _TRUSTED,
    "MoodTool": _TRUSTED,
    "PursueGoal": _TRUSTED,
    "AdvanceGoal": _TRUSTED,
    "NoteToolLesson": _TRUSTED,
    "SelfRevision": _TRUSTED,
    "Report": _TRUSTED,
}


def tools_for(principal: Principal) -> frozenset[str]:
    """Every tool ``principal`` may ever be bound to."""
    return frozenset(n for n, allowed in TOOL_MATRIX.items() if principal in allowed)
