"""Tests for tool_memory (Spec B)."""
from __future__ import annotations
from collections import deque

import pytest

from dollos.cascade.tool_loop import ToolResult
from dollos.mind.mind_state import MindState, ToolFailure
from dollos.mind.tool_memory import record_tool_outcome, render_tool_notes, render_tool_outcomes


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


def test_render_tool_outcomes_has_counts_and_failure_snippet():
    from collections import deque
    from dollos.mind.mind_state import ToolFailure
    from dollos.mind.tool_memory import render_tool_outcomes
    stats = {"Shell": {"ok": 3, "fail": 1}, "Recall": {"ok": 5, "fail": 0}}
    fails = deque([ToolFailure(t=1.0, tool="Shell", detail="timeout after 60s")], maxlen=10)
    out = render_tool_outcomes(stats, fails)
    assert "Shell" in out and "3 ok" in out and "1 fail" in out
    assert "timeout after 60s" in out
    assert "Recall" in out and "5 ok" in out


# --- Task 7: tool_habits_search + render_tool_habits ---


def test_parse_playbook_chunk():
    from dollos.mind.tool_memory import _parse_playbook_chunk
    chunk = "## 2026-06-27 10:00:00\n\n[situation] grepping output\nuse GrepToolOutput\n"
    assert _parse_playbook_chunk(chunk) == ("grepping output", "use GrepToolOutput")
    assert _parse_playbook_chunk("garbage with no situation") is None


@pytest.mark.asyncio
async def test_tool_habits_search_gated_and_source_restricted(tmp_path):
    from dollos.mind.mind_state import MindState
    from dollos.mind.tool_memory import tool_habits_search

    class _FakeMem:
        def __init__(self): self.calls = []
        async def search(self, q, top_k=5, source_prefix=None):
            self.calls.append({"q": q, "top_k": top_k, "source_prefix": source_prefix})
            return [{"content": "[situation] s\nl", "source": str(source_prefix)}]

    pb = tmp_path / "tool_playbook.md"
    s = MindState()
    mem = _FakeMem()
    # gate: no tool_stats → no search
    assert await tool_habits_search(mem, s, pb) == []
    assert mem.calls == []
    # gate: tool_stats present but playbook missing → no search
    s.tool_stats = {"Shell": {"ok": 1, "fail": 0}}
    assert await tool_habits_search(mem, s, pb) == []
    assert mem.calls == []
    # both present → search with source_prefix=playbook
    pb.write_text("## h\n\n[situation] s\nl\n")
    s.focus = "doing things"
    hits = await tool_habits_search(mem, s, pb)
    assert len(mem.calls) == 1
    assert mem.calls[0]["source_prefix"] == str(pb.resolve())
    assert "Shell" in mem.calls[0]["q"] and "doing things" in mem.calls[0]["q"]
    assert hits


def test_render_tool_habits_gated():
    from dollos.mind.tool_memory import render_tool_habits
    assert render_tool_habits([]) is None
    out = render_tool_habits([{"content": "## h\n\n[situation] grep\nuse Grep\n"}])
    assert "[Tool habits]" in out and "grep" in out and "use Grep" in out
