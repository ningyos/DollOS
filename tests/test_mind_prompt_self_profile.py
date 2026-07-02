"""Tests for the [Self profile] always-inject block in render_mind (Task 6)."""
from dollos.mind.mind_prompt import render_mind, _percep_body
from dollos.mind.mind_state import MindState, Perception
import time


def _state():
    return MindState()  # 若 MindState 需參數,沿用既有測試建法


def test_self_profile_block_rendered_before_memory_guideline():
    body = "## 我學到的自己\n- [s1·2026-06-30] 我重視誠實"
    out = render_mind(_state(), [], "SYSTEM", self_profile_text=body)
    assert "[Self profile]" in out
    assert body in out
    # 位置:在 system_prompt 之後、[Memory guideline] 之前
    assert out.index("[Self profile]") < out.index("[Memory guideline]")
    assert out.index("SYSTEM") < out.index("[Self profile]")


def test_self_profile_block_absent_when_none():
    out = render_mind(_state(), [], "SYSTEM", self_profile_text=None)
    assert "[Self profile]" not in out


def test_reflection_nudge_mentions_pinself():
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "PinSelf" in text
    assert "self-profile" in text or "自己" in text


def test_reflection_nudge_grounds_before_introspection():
    """Anti-performativity guard: the grounding clause ('回看實際做過的事')
    must come before any invitation to introspect, so a weak model doesn't
    free-associate ungrounded self-description."""
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert text.index("實際做過的事") < text.index("PinSelf")


def test_reflection_nudge_states_subject_test():
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "主詞" in text


def test_reflection_nudge_allows_write_loose_not_yet_durable():
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "先記" in text
    assert "淘汰會篩" in text


def test_reflection_nudge_keeps_bootstrap_safe_clause():
    """Regression guard for commit b9d87b7 — without this clause the model
    waits for a non-empty [Self profile] before its first pin."""
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "不必等 [Self profile] 已經有內容才動作" in text


def test_reflection_nudge_keeps_operational_calling_hint():
    """Regression guard for commit 70de95a — without the explicit
    (op=add,section 選 ...) hint, nudge salience regresses."""
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "op=add,section 選 self/relationship/user" in text


def test_reflection_nudge_states_notetoollesson_division():
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "NoteToolLesson" in text


def test_self_profile_header_frames_prune_as_selection():
    out = render_mind(_state(), [], "SYSTEM", self_profile_text="- [s1·2026-07-02] test")
    assert "keep only what's still you" in out
