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
from dollos.harness.turn_policy import POLICIES
from dollos.mind.mind_state import Perception
from dollos.tools import KEEPER_TOOLS, MAIN_TOOLS, SUB_TOOLS
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
    for kind in ("agenda", "diary", "pulse", "safe_mode"):
        listed |= POLICIES[kind].kind_tools
    listed |= {"PinSelf", "NoteToolLesson", "SelfRevision", "LearnName", "Report"}
    assert set(TOOL_MATRIX) == listed


def test_owner_dm_row_equals_owner_row():
    assert tools_for("owner_dm") == tools_for("owner")


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
