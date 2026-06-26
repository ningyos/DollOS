"""P10: Perception.kind Literal must cover every kind emitted in src/."""
from typing import get_args, get_type_hints

from dollos.mind.mind_state import Perception


def test_interrupted_in_kind_literal():
    # mind_state uses `from __future__ import annotations`, so the raw
    # dataclass field type is a string; resolve via get_type_hints.
    hints = get_type_hints(Perception)
    kinds = get_args(hints["kind"])
    assert "Interrupted" in kinds  # kernel.py emits kind="Interrupted"


def test_kind_literal_covers_all_emitted_kinds():
    # Every Perception kind produced anywhere in src/ must be a member.
    hints = get_type_hints(Perception)
    kinds = set(get_args(hints["kind"]))
    emitted = {
        "UserSpoke", "ToolResultArrived", "MonitorFired", "MonitorEnded",
        "ScheduledMoment", "Awoke", "ReflectionMoment", "Interrupted",
    }
    assert emitted <= kinds


def test_dead_events_module_is_gone():
    import importlib.util
    assert importlib.util.find_spec("dollos.events") is None
