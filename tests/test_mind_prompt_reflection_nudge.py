"""Tests for the self-directed agenda grounded nudge on ReflectionMoment
turns (self-directed-agenda spec §3.2, Task 4).

Mirrors tests/test_mind_prompt_self_profile.py's ReflectionMoment nudge
test pattern (_percep_body direct assertions), plus a full render_mind
assembly check for presence-on-reflection / absence-on-non-reflection.
"""
import time

from dollos.mind.mind_prompt import _percep_body, render_mind
from dollos.mind.mind_state import MindState, Perception


def _reflection_perception():
    return Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})


def test_reflection_nudge_contains_agenda_grounding_wording():
    text = _percep_body(_reflection_perception())
    assert "回顧" in text
    assert "自己想追下去的" in text
    assert "PursueGoal" in text


def test_reflection_nudge_allows_finding_nothing():
    """Anti-performativity guard (project_character_acting): the nudge must
    read as 'look back at what's real, skip it if there's nothing' — never
    'you have goals, go pursue them', which would manufacture empty
    performed goals."""
    text = _percep_body(_reflection_perception())
    assert "沒有就算了" in text
    assert "不用硬找" in text


def test_reflection_nudge_asks_for_provenance():
    """PursueGoal's required `trigger` field must be self-explained — the
    nudge tells her to say where the pursuit came from."""
    text = _percep_body(_reflection_perception())
    assert "說清楚它從哪來" in text


def test_reflection_nudge_appears_after_existing_pinself_content():
    """The new agenda nudge is appended after the load-bearing PinSelf /
    SelfRevision division-of-labor content already in the ReflectionMoment
    branch, not inserted before/inside it."""
    text = _percep_body(_reflection_perception())
    assert text.index("SelfRevision") < text.index("PursueGoal")


def test_non_reflection_perception_has_no_agenda_nudge():
    p = Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"})
    text = _percep_body(p)
    assert "PursueGoal" not in text
    assert "自己想追下去的" not in text


def test_reflection_turn_prompt_contains_agenda_nudge():
    """Full render_mind assembly: a ReflectionMoment perception in
    recent_perceptions makes the agenda nudge appear in the composed
    prompt (it renders inside [Recent perceptions])."""
    state = MindState()
    state.recent_perceptions.append(_reflection_perception())
    out = render_mind(state, [], "SYSTEM")
    assert "PursueGoal" in out
    assert "自己想追下去的" in out


def test_non_reflection_turn_prompt_has_no_agenda_nudge():
    state = MindState()
    state.recent_perceptions.append(
        Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"})
    )
    out = render_mind(state, [], "SYSTEM")
    assert "PursueGoal" not in out
    assert "自己想追下去的" not in out
