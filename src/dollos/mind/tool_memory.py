"""Tool memory — per-tool outcome stats, recent-failure notes, and the
append-only tool-lesson playbook surfacing (Spec B)."""
from __future__ import annotations

import logging
import time
from collections import deque

from dollos.mind.mind_state import MindState, ToolFailure

logger = logging.getLogger(__name__)

TOOL_NOTE_WINDOW_S = 3600.0  # only surface failures from the last hour
_MAX_TOOL_NOTES = 5
_DETAIL_CAP = 100


def record_tool_outcome(state: MindState, name: str, result) -> None:
    """Record one tool dispatch outcome. Observability only — never raises.

    result: None (side-effect tool ran cleanly) or a ToolResult.
    """
    try:
        stat = state.tool_stats.setdefault(name, {"ok": 0, "fail": 0})
        if result is None or result.success:
            stat["ok"] += 1
        else:
            stat["fail"] += 1
            state.recent_tool_failures.append(
                ToolFailure(t=time.time(), tool=name, detail=(result.detail or "")[:200])
            )
    except Exception:
        logger.exception("record_tool_outcome failed for %s; continuing", name)


_MAX_OUTCOME_FAILS = 3
_OUTCOME_DETAIL_CAP = 100


def render_tool_outcomes(tool_stats: dict, recent_tool_failures: deque) -> str:
    """[Tool outcomes since last reflection] — per-tool ok/fail + recent fail samples.
    Reflection-only; caller gates on is_reflection."""
    lines = ["[Tool outcomes since last reflection]"]
    last_fail: dict[str, str] = {}
    for f in list(recent_tool_failures)[-_MAX_OUTCOME_FAILS:]:
        last_fail[f.tool] = f.detail[:_OUTCOME_DETAIL_CAP]
    for tool, st in tool_stats.items():
        line = f"- {tool}: {st.get('ok', 0)} ok, {st.get('fail', 0)} fail"
        if tool in last_fail:
            line += f" — last fail: {last_fail[tool]}"
        lines.append(line)
    return "\n".join(lines[:20])


def render_tool_notes(recent_tool_failures: deque, now: float) -> str | None:
    """[Tool notes] block from recent failures, or None when none are recent.

    Window-filtered, deduped by tool (latest wins), newest-first, capped.
    """
    recent = [f for f in recent_tool_failures if now - f.t < TOOL_NOTE_WINDOW_S]
    if not recent:
        return None
    latest: dict[str, ToolFailure] = {}
    for f in recent:
        if f.tool not in latest or f.t > latest[f.tool].t:
            latest[f.tool] = f
    ordered = sorted(latest.values(), key=lambda f: f.t, reverse=True)[:_MAX_TOOL_NOTES]
    lines = [f"- {f.tool}: {f.detail[:_DETAIL_CAP]}" for f in ordered]
    return "[Tool notes] 最近工具失敗（避免重蹈同樣錯誤）：\n" + "\n".join(lines)
