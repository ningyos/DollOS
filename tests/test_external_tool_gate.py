"""P1e Task 2 (S4/S5): conservative external tool registry + keyed grammar
cache.

Any external-origin turn (external_public OR external_dm, i.e. INCLUDING the
owner's own DM) must get a structurally reduced tool set — Shell,
SpawnWorkflow, SpawnMonitor, RemoveMonitor, InvokeSkill, WriteSchedule, and
SelfRevision must never appear, no matter what safe_mode/reflection/evolution
flags are set. This is the hard security boundary: Discord account theft must
never equal home-computer RCE.

S5: the external reduction must WIN OVER reflection expansion — an external
turn that is also a ReflectionMoment gets PinSelf (safe, self-profile) but
never SelfRevision (慢變演化 self-rewrite) or Shell.
"""
from __future__ import annotations

from tests._mindloop_factory import make_mindloop

# Full set that must NEVER appear on any external-origin turn — including the
# owner's own DM (brief: "owner 也砍"). SelfRevision (慢變演化 self-rewrite) is
# in here too: an external reflection turn must not regain it.
DANGEROUS = ("Shell", "SpawnWorkflow", "SpawnMonitor", "RemoveMonitor",
             "InvokeSkill", "WriteSchedule", "SelfRevision")


def test_external_public_registry_is_conservative(tmp_path):
    ml = make_mindloop(memory_root=tmp_path)
    ml._ctx.origin_tier = "external_public"
    reg = ml._active_tool_registry()
    assert set(reg.keys()) <= {"Recall", "NoteMemory", "WriteDiary", "PinSelf"}
    for name in DANGEROUS:
        assert name not in reg


def test_external_dm_owner_registry_is_conservative_too(tmp_path):
    """Owner's own DM is still external — no home-computer RCE via a
    compromised Discord account, even the owner's."""
    ml = make_mindloop(memory_root=tmp_path)
    ml._ctx.origin_tier = "external_dm"
    reg = ml._active_tool_registry()
    assert set(reg.keys()) <= {"Recall", "NoteMemory", "WriteDiary", "PinSelf"}
    for name in DANGEROUS:
        assert name not in reg


def test_internal_turn_keeps_full_registry(tmp_path):
    ml = make_mindloop(memory_root=tmp_path)
    ml._ctx.origin_tier = "internal"
    reg = ml._active_tool_registry()
    assert "Shell" in reg
    assert "SpawnWorkflow" in reg
    assert "WriteSchedule" in reg


def test_external_reflection_excludes_selfrevision_and_shell(tmp_path):
    """S5 critical: reduction wins over reflection expansion."""
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True,
                        self_profile_enabled=True)
    ml._ctx.origin_tier = "external_public"
    ml._is_reflection = True
    reg = ml._active_tool_registry()
    assert "PinSelf" in reg              # reflection expansion kept (safe)
    for name in DANGEROUS:                # S5: full dangerous set stays out
        assert name not in reg


def test_external_dm_reflection_excludes_selfrevision_and_shell(tmp_path):
    """Owner-DM reflection turn (brief: "owner 也砍"): same teeth as the
    external_public sibling — the FULL dangerous set stays out, only PinSelf
    is regained."""
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True,
                        self_profile_enabled=True)
    ml._ctx.origin_tier = "external_dm"
    ml._is_reflection = True
    reg = ml._active_tool_registry()
    assert "PinSelf" in reg
    for name in DANGEROUS:                # S5 parity: full dangerous set out
        assert name not in reg


def test_external_reflection_without_self_profile_has_no_pinself(tmp_path):
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True,
                        self_profile_enabled=False)
    ml._ctx.origin_tier = "external_public"
    ml._is_reflection = True
    reg = ml._active_tool_registry()
    assert "PinSelf" not in reg
    assert "SelfRevision" not in reg


def test_internal_reflection_turn_keeps_selfrevision_control(tmp_path):
    """Control group: internal reflection turn still gets SelfRevision when
    evolution is enabled — proves the external branch, not some global
    change, is what suppresses it."""
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True,
                        self_profile_enabled=True)
    ml._ctx.origin_tier = "internal"
    ml._is_reflection = True
    reg = ml._active_tool_registry()
    assert "SelfRevision" in reg
    assert "PinSelf" in reg


def test_safe_mode_wins_over_external_too(tmp_path):
    """safe_mode still has top priority (checked first) — combining safe_mode
    with an external origin must not somehow re-add tools."""
    ml = make_mindloop(memory_root=tmp_path)
    ml._ctx.origin_tier = "external_public"
    ml._state.safe_mode = True
    reg = ml._active_tool_registry()
    assert set(reg.keys()) <= {"Recall", "ReadToolOutput", "GrepToolOutput"}
    for name in DANGEROUS:
        assert name not in reg


def test_external_grammar_is_non_none_and_differs_from_internal(tmp_path):
    ml = make_mindloop(memory_root=tmp_path)
    ml._ctx.origin_tier = "internal"
    internal_grammar = ml._active_grammar()

    ml._ctx.origin_tier = "external_public"
    external_grammar = ml._active_grammar()

    assert external_grammar is not None
    assert "root ::=" in external_grammar
    assert external_grammar != internal_grammar


def test_external_grammar_cache_returns_same_object_on_repeat_call(tmp_path):
    """Keyed cache: same tier repeat call returns the SAME cached grammar
    object, not a freshly rebuilt one."""
    ml = make_mindloop(memory_root=tmp_path)
    ml._ctx.origin_tier = "external_public"
    first = ml._active_grammar()
    second = ml._active_grammar()
    assert first is second
