"""Principal and permission table (harness spec §6.3/§6.5, plan 2026-10-27
Task 2 / S2).

- ``principal_of``: the lowest-trust authored input of a batch wins; a
  missing ``is_dm`` / ``author_is_owner`` fails closed to external_public.
- ``TOOL_MATRIX`` reproduces today's binding, with the one change of
  2026-10-27: owner DM = the owner at the local keyboard.
- An owner DM can run Shell; the same message in a public channel, or from a
  non-owner, gets Shell refused.
"""
from __future__ import annotations

import time

import pytest

from dollos.harness.permissions import (
    ORIGIN_TIER,
    TOOL_MATRIX,
    TRUST_ORDER,
    principal_of,
    tools_for,
)
from dollos.mind.mind_loop import SAFE_MODE_TOOLS
from dollos.mind.mind_state import Perception
from dollos.tools import (
    AGENDA_TOOLS,
    DIARY_TOOLS,
    KEEPER_TOOLS,
    MAIN_TOOLS,
    PULSE_TOOLS,
    SUB_TOOLS,
)
from tests._mindloop_factory import make_mindloop


def _p(kind: str, **data) -> Perception:
    return Perception(kind=kind, t=time.time(), data=data)


def _cm(**data) -> Perception:
    return _p("ChannelMessage", channel_id="c1", content="hi", **data)


OWNER_DM = _cm(author_is_owner=True, is_dm=True)
OWNER_PUBLIC = _cm(author_is_owner=True, is_dm=False)
STRANGER_DM = _cm(author_is_owner=False, is_dm=True)
STRANGER_PUBLIC = _cm(author_is_owner=False, is_dm=False)
USER = _p("UserSpoke", text="hi")


# ---------------------------------------------------------------------------
# principal_of
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("batch", "principal"), [
    ([OWNER_DM], "owner_dm"),
    ([OWNER_PUBLIC], "external_public"),
    ([STRANGER_DM], "external_public"),
    ([STRANGER_PUBLIC], "external_public"),
    ([_cm(author_is_owner=True)], "external_public"),           # no is_dm
    ([_cm(is_dm=True)], "external_public"),                     # no author_is_owner
    ([_cm(author_is_owner=True, is_dm=None)], "external_public"),
    ([_cm()], "external_public"),
    ([OWNER_DM, STRANGER_PUBLIC], "external_public"),
    ([STRANGER_PUBLIC, OWNER_DM], "external_public"),           # order-free
    ([OWNER_DM, OWNER_PUBLIC], "external_public"),
    ([USER], "owner"),
    ([USER, OWNER_DM], "owner_dm"),
    ([OWNER_DM, USER], "owner_dm"),
    ([USER, STRANGER_PUBLIC], "external_public"),
    ([USER, _p("ReflectionMoment", iters_since_last=5)], "owner"),
    ([USER, _p("ToolResultArrived")], "owner"),
    ([_p("ReflectionMoment", iters_since_last=5)], "internal"),
    ([_p("DiaryMoment")], "internal"),
    ([_p("AgendaMoment")], "internal"),
    ([_p("PulseMoment")], "internal"),
    ([_p("ToolResultArrived")], "internal"),
    ([_p("ScheduledMoment")], "internal"),
    ([OWNER_DM, _p("ToolResultArrived")], "owner_dm"),
    ([], "internal"),
])
def test_principal_of(batch, principal):
    assert principal_of(batch) == principal


def test_trust_order_and_origin_tier_compat():
    assert TRUST_ORDER == ("owner", "owner_dm", "internal", "external_public")
    assert ORIGIN_TIER == {
        "owner": "internal",
        "owner_dm": "external_dm",
        "internal": "internal",
        "external_public": "external_public",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("batch", "principal", "tier"), [
    ([USER], "owner", "internal"),
    ([OWNER_DM], "owner_dm", "external_dm"),
    ([OWNER_PUBLIC], "external_public", "external_public"),
    ([STRANGER_DM], "external_public", "external_public"),
    ([_p("ReflectionMoment", iters_since_last=5)], "internal", "internal"),
])
async def test_turn_sets_principal_and_origin_tier(tmp_path, batch, principal, tier):
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn(batch)
    assert ml._ctx.principal == principal
    assert ml._ctx.origin_tier == tier


# ---------------------------------------------------------------------------
# TOOL_MATRIX
# ---------------------------------------------------------------------------


def test_matrix_has_a_row_for_every_tool():
    listed = {c.__name__ for c in MAIN_TOOLS + SUB_TOOLS + KEEPER_TOOLS}
    listed |= AGENDA_TOOLS | DIARY_TOOLS | PULSE_TOOLS | SAFE_MODE_TOOLS
    listed |= {"PinSelf", "NoteToolLesson", "SelfRevision", "LearnName", "Report"}
    assert set(TOOL_MATRIX) == listed


def test_owner_dm_row_equals_owner_row():
    assert tools_for("owner_dm") == tools_for("owner")


# principal → today's (origin_tier, _has_user_spoke) that selects it.
_STATE = {
    "owner": ("internal", True),
    "owner_dm": ("external_dm", False),
    "internal": ("internal", False),
    "external_public": ("external_public", False),
}
_MAIN = frozenset(c.__name__ for c in MAIN_TOOLS)

# Every principal × turn kind today's tests exercise (test_external_tool_gate,
# test_mind_loop_learnname_gate, the agenda/diary/pulse turn tests). Diary,
# agenda and pulse are pure-batch kinds, so only internal reaches them.
_SITUATIONS = [
    *((pr, k) for pr in ("owner", "owner_dm", "internal", "external_public")
      for k in ("normal", "reflection", "safe_mode")),
    ("internal", "diary"), ("internal", "agenda"), ("internal", "pulse"),
]


def _kind_tools(kind: str, self_profile: bool, evolution: bool) -> frozenset[str]:
    """Today's turn-kind narrowing, without the principal."""
    if kind == "safe_mode":
        return SAFE_MODE_TOOLS
    if kind == "diary":
        return DIARY_TOOLS
    if kind == "agenda":
        return AGENDA_TOOLS
    if kind == "pulse":
        return PULSE_TOOLS
    tools = set(_MAIN) | {"LearnName"}
    if kind == "reflection":
        tools.add("NoteToolLesson")
        if self_profile:
            tools.add("PinSelf")
        if evolution:
            tools.add("SelfRevision")
    return frozenset(tools)


def _bound(tmp_path, principal: str, kind: str, self_profile: bool, evolution: bool):
    ml = make_mindloop(memory_root=tmp_path, self_profile_enabled=self_profile,
                       evolution_enabled=evolution)
    ml._ctx.origin_tier, ml._has_user_spoke = _STATE[principal]
    ml._ctx.principal = principal
    ml._state.safe_mode = kind == "safe_mode"
    ml._is_reflection = kind == "reflection"
    ml._is_diary = kind == "diary"
    ml._is_agenda = kind == "agenda"
    ml._is_pulse = kind == "pulse"
    return set(ml._active_tool_registry()), set(ml._runnable_tools(ml._offered_tools()))


@pytest.mark.parametrize(("self_profile", "evolution"), [(True, True), (False, False)])
@pytest.mark.parametrize(("principal", "kind"), _SITUATIONS)
def test_matrix_matches_todays_binding(tmp_path, principal, kind, self_profile, evolution):
    """TOOL_MATRIX ∩ today's kind narrowing == what the turn may run.

    Compared against ``_runnable_tools(_offered_tools())`` (what can actually
    run), which equals ``_active_tool_registry()`` everywhere except two
    external_public cases where the registry names a tool the tier is never
    offered, so it never runs: safe mode (ReadToolOutput / GrepToolOutput)
    and reflection with self_profile (PinSelf).

    Owner DM must equal the local owner (user decision 2026-10-27).

    DELETE after plan 2026-10-27 Task 3: once _active_tool_registry is a
    TurnPolicy ∩ TOOL_MATRIX lookup, this comparison is circular.
    """
    active, runnable = _bound(tmp_path, principal, kind, self_profile, evolution)
    expected = _kind_tools(kind, self_profile, evolution) & tools_for(principal)
    assert runnable == expected
    if principal == "external_public" and kind == "safe_mode":
        assert active - runnable == {"ReadToolOutput", "GrepToolOutput"}
    elif principal == "external_public" and kind == "reflection" and self_profile:
        assert active - runnable == {"PinSelf"}
    else:
        assert active == runnable
    if principal == "owner_dm":
        owner_active, owner_runnable = _bound(
            tmp_path, "owner", kind, self_profile, evolution
        )
        assert (active, runnable) == (owner_active, owner_runnable)


# ---------------------------------------------------------------------------
# Owner DM runs Shell; public channel / non-owner is refused
# ---------------------------------------------------------------------------


class _FakeShellRunner:
    def __init__(self):
        self.spawned: list[str] = []

    def spawn(self, *, command, timeout_s, response_sink):
        self.spawned.append(command)


async def _shell_after(tmp_path, message: Perception):
    ml = make_mindloop(memory_root=tmp_path)
    runner = _FakeShellRunner()
    ml._ctx.shell_runner = runner
    await ml._run_one_turn([message])
    r = await ml._dispatch_bound(
        "Shell", {"command": "echo hi"}, ml._offered_tools()
    )
    return r, runner.spawned


@pytest.mark.asyncio
async def test_owner_dm_can_run_shell(tmp_path):
    r, spawned = await _shell_after(tmp_path, OWNER_DM)
    assert r.success
    assert spawned == ["echo hi"]


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [OWNER_PUBLIC, STRANGER_DM, STRANGER_PUBLIC])
async def test_public_channel_or_non_owner_shell_refused(tmp_path, message):
    r, spawned = await _shell_after(tmp_path, message)
    assert not r.success
    assert spawned == []
