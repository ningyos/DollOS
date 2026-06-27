"""Tests for tool_memory (Spec B)."""
from __future__ import annotations
from collections import deque
from dollos.cascade.tool_loop import ToolResult
from dollos.mind.mind_state import MindState, ToolFailure
from dollos.mind.tool_memory import record_tool_outcome, render_tool_notes


def test_record_success_none_and_fail():
    s = MindState()
    record_tool_outcome(s, "Recall", ToolResult("Recall", True, "hit"))
    record_tool_outcome(s, "Shell", None)  # side-effect ok
    record_tool_outcome(s, "ReadToolOutput", ToolResult("ReadToolOutput", False, "limit 需 1–500"))
    assert s.tool_stats["Recall"] == {"ok": 1, "fail": 0}
    assert s.tool_stats["Shell"] == {"ok": 1, "fail": 0}
    assert s.tool_stats["ReadToolOutput"] == {"ok": 0, "fail": 1}
    assert len(s.recent_tool_failures) == 1
    assert s.recent_tool_failures[0].tool == "ReadToolOutput"


def test_record_never_raises_on_bad_result():
    s = MindState()
    record_tool_outcome(s, "X", object())  # no .success attr → swallowed
    # no exception; nothing recorded for the bad path is fine


def test_render_tool_notes_gated_aged_deduped():
    now = 1000.0
    fails = deque([
        ToolFailure(t=now - 10, tool="Shell", detail="timeout A"),
        ToolFailure(t=now - 5, tool="Shell", detail="timeout B"),   # newer Shell → wins dedup
        ToolFailure(t=now - 99999, tool="OldTool", detail="ancient"),  # aged out
    ], maxlen=10)
    out = render_tool_notes(fails, now)
    assert out is not None
    assert "timeout B" in out and "timeout A" not in out  # dedup keeps latest
    assert "OldTool" not in out  # aged out (>1h)
    assert render_tool_notes(deque(maxlen=10), now) is None  # no failures → no block
