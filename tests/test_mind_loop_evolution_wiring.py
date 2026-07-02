"""MindLoop wires SelfRevision into the reflection registry/grammar + latch reset."""
from dollos.tools import SelfRevision


def test_reflection_registry_includes_self_revision_when_enabled(tmp_path):
    from tests._mindloop_factory import make_mindloop
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True)
    ml._is_reflection = True
    ml._state.safe_mode = False
    assert "SelfRevision" in ml._active_tool_registry()


def test_reflection_registry_excludes_self_revision_when_disabled(tmp_path):
    from tests._mindloop_factory import make_mindloop
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=False)
    ml._is_reflection = True
    ml._state.safe_mode = False
    assert "SelfRevision" not in ml._active_tool_registry()


def test_safe_mode_excludes_self_revision(tmp_path):
    from tests._mindloop_factory import make_mindloop
    ml = make_mindloop(memory_root=tmp_path, evolution_enabled=True)
    ml._is_reflection = True
    ml._state.safe_mode = True
    assert "SelfRevision" not in ml._active_tool_registry()


def test_self_revision_in_refeed_allowlist():
    from dollos.mind.mind_loop import IN_TURN_REFEED_TOOLS
    assert "SelfRevision" in IN_TURN_REFEED_TOOLS
