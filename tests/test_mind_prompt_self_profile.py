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
