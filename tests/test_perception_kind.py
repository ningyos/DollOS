"""P10: Perception.kind Literal must cover every kind emitted in src/."""
from typing import get_args, get_type_hints

from dollos.mind.mind_state import Perception


def test_kind_literal_covers_all_emitted_kinds():
    # Every Perception kind produced anywhere in src/ must be a member.
    hints = get_type_hints(Perception)
    kinds = set(get_args(hints["kind"]))
    emitted = {
        "UserSpoke", "ToolResultArrived",
        "ScheduledMoment", "Awoke", "ReflectionMoment", "Interrupted",
    }
    assert emitted <= kinds
