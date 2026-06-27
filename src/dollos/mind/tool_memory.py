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
