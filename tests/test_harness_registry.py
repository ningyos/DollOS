"""ToolRegistry (plan 2026-10-27 Task 4, S5): ``offered`` is today's fixed
list and tool JSON, ``bound`` is ``turn_policy.bound_tools``, and each tool
declares ``effectful``."""
from __future__ import annotations

import pytest

from dollos.harness.permissions import TRUST_ORDER
from dollos.harness.registry import EXTRA_TOOL_CLASSES, ToolRegistry
from dollos.harness.turn_policy import offered_tools
from dollos.llm.tool_schema import tool_schemas
from dollos.tools import KEEPER_TOOLS, MAIN_TOOLS, SUB_TOOLS, Recall, Shell
from tests._mindloop_factory import make_mindloop
from tests.test_harness_turn_policy import _EVERYTHING_OFF, _EVERYTHING_ON, _OFFERED

_MAIN = {c.__name__: c for c in MAIN_TOOLS}
_CONFIGS = [(True, True), (True, False), (False, True), (False, False)]


@pytest.mark.parametrize(("self_profile", "evolution"), _CONFIGS)
@pytest.mark.parametrize("principal", TRUST_ORDER)
def test_offered_is_todays_list_and_json(tmp_path, principal, self_profile, evolution):
    o = ToolRegistry(_MAIN).offered(principal, self_profile=self_profile,
                                     evolution=evolution)
    names = sorted(offered_tools(principal, _MAIN, self_profile=self_profile,
                                 evolution=evolution))
    assert list(o.tools) == names
    classes = {**_MAIN, **EXTRA_TOOL_CLASSES}
    assert o.schemas == tool_schemas(classes[n] for n in names)
    if (self_profile, evolution) in _OFFERED:
        assert set(o.tools) == _OFFERED[(self_profile, evolution)][principal]
    ml = make_mindloop(memory_root=tmp_path, self_profile_enabled=self_profile,
                       evolution_enabled=evolution)
    ml._ctx.principal = principal
    assert ml._offered_tools() == o.tools


@pytest.mark.parametrize(
    ("self_profile", "evolution", "situation", "bound"),
    [(True, True, row[0], row[1]) for row in _EVERYTHING_ON]
    + [(False, False, row[0], row[1]) for row in _EVERYTHING_OFF],
)
def test_bound_matches_turn_policy(self_profile, evolution, situation, bound):
    kind, principal = situation
    got = ToolRegistry(_MAIN).bound(kind, principal, self_profile=self_profile,
                                    evolution=evolution)
    assert set(got) == bound
    assert list(got) == sorted(got)


@pytest.mark.parametrize(("kind", "tools"), [("worker", SUB_TOOLS), ("keeper", KEEPER_TOOLS)])
def test_agent_kinds_bind_their_whole_toolkit(kind, tools):
    by_name = {c.__name__: c for c in tools}
    got = ToolRegistry(by_name).bound(kind, "internal", self_profile=False, evolution=False)
    assert got == dict(sorted(by_name.items()))


def test_register_adds_and_refuses_a_taken_name():
    reg = ToolRegistry({"Recall": Recall})
    reg.register("Shell", Shell)
    assert reg["Shell"] is Shell
    with pytest.raises(ValueError):
        reg.register("Recall", Recall)
    with pytest.raises(ValueError):
        reg.register("PinSelf", Shell)


def test_effectful_defaults_true_and_read_only_tools_say_false():
    every = {**_MAIN, **EXTRA_TOOL_CLASSES, **{c.__name__: c for c in SUB_TOOLS}}
    reg = ToolRegistry(every)
    read_only = {n for n in every if not reg.effectful(n)}
    assert read_only == {"Recall", "ReadToolOutput", "GrepToolOutput", "InvokeSkill"}


def test_effectful_is_not_in_the_tool_json():
    for schema in tool_schemas([*MAIN_TOOLS, *SUB_TOOLS]):
        assert "effectful" not in str(schema)
