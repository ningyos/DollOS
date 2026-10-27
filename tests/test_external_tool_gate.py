"""P1e Task 2 (S4/S5): conservative external tool registry, and the fixed
per-tier tool list the model is shown (plan 2026-10-18 decision 3).

An external_public turn (strangers, non-owner DMs, the owner posting in a
PUBLIC channel) must get a structurally reduced tool set — Shell,
SpawnWorkflow, SpawnMonitor, RemoveMonitor, InvokeSkill, WriteSchedule, and
SelfRevision must never appear, no matter what safe_mode/reflection/evolution
flags are set. This is the hard security boundary.

The owner's own DM (external_dm) is NOT reduced any more: owner DM = the
owner at the local keyboard (user decision 2026-10-27, plan 2026-10-27
Task 2; the compromised-Discord-account risk was accepted by the user).

S5: the external reduction must WIN OVER reflection expansion — an external
turn that is also a ReflectionMoment gets PinSelf (safe, self-profile) but
never SelfRevision (慢變演化 self-rewrite) or Shell.
"""
from __future__ import annotations

from tests._mindloop_factory import make_mindloop

# Full set that must NEVER appear on an external_public turn. SelfRevision
# (慢變演化 self-rewrite) is in here too: an external reflection turn must not
# regain it. (The owner's DM gets them since 2026-10-27, like the local owner.)
DANGEROUS = ("Shell", "SpawnWorkflow", "SpawnMonitor", "RemoveMonitor",
             "InvokeSkill", "WriteSchedule", "SelfRevision")


def test_external_public_registry_is_conservative(tmp_path):
    ml = make_mindloop(memory_root=tmp_path)
    ml._ctx.origin_tier = "external_public"
    reg = ml._active_tool_registry()
    assert set(reg.keys()) <= {"Recall", "NoteMemory", "WriteDiary", "PinSelf"}
    for name in DANGEROUS:
        assert name not in reg


def test_external_dm_owner_registry_equals_local_owner(tmp_path):
    """Owner's own DM = the owner at the local keyboard (user decision
    2026-10-27): the same registry as an internal turn with a live UserSpoke,
    LearnName and Shell included. The owner in a PUBLIC channel stays
    external_public and conservative."""
    ml = make_mindloop(memory_root=tmp_path)
    ml._ctx.origin_tier = "internal"
    ml._has_user_spoke = True
    local_owner = set(ml._active_tool_registry())
    ml._has_user_spoke = False
    ml._ctx.origin_tier = "external_dm"
    reg = ml._active_tool_registry()
    assert set(reg.keys()) == local_owner
    assert "LearnName" in reg
    assert "Shell" in reg
    ml._ctx.origin_tier = "external_public"
    reg = ml._active_tool_registry()
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


def test_external_dm_reflection_equals_owner_reflection(tmp_path):
    """Owner-DM reflection turn (user decision 2026-10-27): bound like the
    local owner's reflection turn — SelfRevision and Shell included — while
    the external_public sibling keeps the FULL dangerous set out."""
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True,
                        self_profile_enabled=True)
    ml._is_reflection = True
    ml._ctx.origin_tier = "internal"
    ml._has_user_spoke = True
    owner_reflection = set(ml._active_tool_registry())
    ml._has_user_spoke = False
    ml._ctx.origin_tier = "external_dm"
    reg = ml._active_tool_registry()
    assert set(reg) == owner_reflection
    assert "PinSelf" in reg
    for name in DANGEROUS:
        assert name in reg
    ml._ctx.origin_tier = "external_public"
    reg = ml._active_tool_registry()
    for name in DANGEROUS:                # S5: full dangerous set out
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


def test_external_offered_tools_hide_every_internal_tool(tmp_path):
    """The schema list an external_public turn is shown never names an
    internal tool — not even on a reflection turn, with evolution and
    self-profile on. The owner's DM is shown the internal list (2026-10-27)."""
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True,
                        self_profile_enabled=True)
    ml._ctx.origin_tier = "internal"
    internal = set(ml._offered_tools())
    for reflection in (False, True):
        ml._is_reflection = reflection
        ml._ctx.origin_tier = "external_public"
        offered = set(ml._offered_tools())
        assert offered == {"Recall", "NoteMemory", "WriteDiary"}
        for name in DANGEROUS:
            assert name not in offered
        ml._ctx.origin_tier = "external_dm"
        assert set(ml._offered_tools()) == internal
    assert set(DANGEROUS) <= internal


def test_offered_tools_cover_every_binding_of_the_tier(tmp_path):
    """Every tool a turn of the tier can be bound to is in the tier's offered
    list (so binding is only ever a refusal, never a missing schema)."""
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True,
                        self_profile_enabled=True)
    ml._ctx.origin_tier = "internal"
    offered = set(ml._offered_tools())
    for flag in ("_is_reflection", "_is_diary", "_is_agenda", "_is_pulse", "_has_user_spoke"):
        setattr(ml, flag, True)
        assert set(ml._active_tool_registry()) <= offered, flag
        setattr(ml, flag, False)
    ml._is_reflection = ml._has_user_spoke = True
    assert set(ml._active_tool_registry()) <= offered
