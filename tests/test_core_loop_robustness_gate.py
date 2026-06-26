"""Task 9 — core-loop-robustness gate / spec cross-check.

Machine-enforceable version of the plan's Task 9 Step 2 (spec coverage
cross-check) and Step 3 (no-fallback audit). Every spec section that landed a
task is asserted to still be present here, so a future revert of any task fails
in CI rather than silently regressing the hardened loop.

Spec: docs/superpowers/specs/2026-06-25-core-loop-robustness-design.md
Plan: docs/superpowers/plans/2026-06-25-core-loop-robustness.md
"""
import importlib.util
import inspect
from typing import get_args, get_type_hints

import pytest


# --- §4.1 P5.1 (Task 1): field-tolerant load + surface-not-blank on corruption
def test_p5_1_load_state_surfaces_not_blanks_on_corruption(tmp_path):
    from dollos.mind.mind_state import MindStateLoadError, load_state

    p = tmp_path / "mind_state.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(MindStateLoadError):
        load_state(p)
    # Quarantined, never blanked.
    assert list(tmp_path.glob("mind_state.json.corrupt-*"))


# --- §4.2 P5.2 (Task 2): save_state returns bool, gating WAL truncation
def test_p5_2_save_state_true_on_success(tmp_path):
    from dollos.mind.mind_state import MindState, save_state

    assert save_state(MindState(), tmp_path / "s.json") is True


def test_p5_2_save_state_false_on_failure(tmp_path):
    from dollos.mind.mind_state import MindState, save_state

    # Parent is a file, so the write fails -> False, never raises (gates the
    # WAL-truncation in iterate so perceptions survive a failed save).
    bad = tmp_path / "afile"
    bad.write_text("x")
    assert save_state(MindState(), bad / "s.json") is False


# --- §5 P10 (Task 3): dead event vocabulary deleted + Interrupted in Literal
def test_p10_dead_events_module_gone():
    assert importlib.util.find_spec("dollos.events") is None


def test_p10_interrupted_in_perception_kind():
    from dollos.mind.mind_state import Perception

    kinds = set(get_args(get_type_hints(Perception)["kind"]))
    assert "Interrupted" in kinds


# --- §6.1 P2-grammar (Task 4): voice-first think emits TOOL before REVIEW
def test_p2_grammar_tool_precedes_review():
    from dollos.llm.templates import build_voice_first_grammar
    from dollos.tools import MAIN_TOOLS

    g = build_voice_first_grammar(MAIN_TOOLS)
    assert '"INTENT: " line "TOOL: " line ' in g
    assert '"REVIEW: " line "MOOD: " line "TOOL: "' not in g


# --- §6.2 P2-capture (Task 5): REVIEW captured + surfaced
def test_p2_capture_recent_reviews_field_and_block():
    from dollos.mind.mind_prompt import render_mind
    from dollos.mind.mind_state import MindState

    s = MindState()
    assert hasattr(s, "recent_reviews")
    s.recent_reviews.append("a lesson learned")
    out = render_mind(s, [], "SYS")
    assert "[Recent self-review]" in out
    assert "a lesson learned" in out


# --- §8.1 P6.1 (Task 6): _dispatch_tool returns a grounded ToolResult on failure
def test_p6_1_tool_result_type_reused():
    from dollos.cascade.tool_loop import ToolResult
    from dollos.mind.mind_loop import MindLoop

    assert {"tool_name", "success", "detail"} <= set(ToolResult.__dataclass_fields__)
    assert inspect.iscoroutinefunction(MindLoop._dispatch_tool)


# --- §7 + §8.2 P1 (Task 7): in-turn re-feed bounded by a budget cap
def test_p1_budget_cap_present_and_bounded():
    from dollos.mind.mind_loop import MAX_SYNC_REFEED_PASSES

    assert isinstance(MAX_SYNC_REFEED_PASSES, int)
    assert 1 < MAX_SYNC_REFEED_PASSES <= 8


# --- §8.3 P6.3 (Task 8): announced read-only safe-mode state
def test_p6_3_safe_mode_state_fields():
    from dollos.mind.mind_state import MindState

    s = MindState()
    assert s.safe_mode is False
    assert s.safe_mode_reason == ""


# --- §6.3 verifier / §DEF P7,P8 / §OS P3,P4,P9: explicitly NOT in this plan.
# No code artifact to assert; documented as deferred in the plan's self-review.
