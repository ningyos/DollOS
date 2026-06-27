"""Tool memory — per-tool outcome stats, recent-failure notes, and the
append-only tool-lesson playbook surfacing (Spec B)."""
from __future__ import annotations

import logging
import time
from collections import deque
from pathlib import Path

from dollos.mind.mind_state import MindState, ToolFailure

logger = logging.getLogger(__name__)

TOOL_NOTE_WINDOW_S = 3600.0  # only surface failures from the last hour
_MAX_TOOL_NOTES = 5
_DETAIL_CAP = 100


def record_tool_outcome(state: MindState, name: str, result) -> None:
    """Record one tool dispatch outcome. Observability only — never raises.

    result: None (side-effect tool ran cleanly) or a ToolResult.
    setdefault is intentionally placed AFTER result.success is accessed so a
    malformed result that raises AttributeError leaves no zombie entry in tool_stats.
    """
    try:
        ok = result is None or result.success
        stat = state.tool_stats.setdefault(name, {"ok": 0, "fail": 0})
        if ok:
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


def render_tool_outcomes(tool_stats: dict, recent_tool_failures: deque) -> str | None:
    """[Tool outcomes since last reflection] — per-tool ok/fail + recent fail samples.
    Reflection-only; caller gates on is_reflection.
    Returns None when tool_stats is empty (consistent with render_tool_notes/render_tool_habits)."""
    if not tool_stats:
        return None
    lines = ["[Tool outcomes since last reflection]"]
    last_fail: dict[str, str] = {}
    for f in list(recent_tool_failures)[-_MAX_OUTCOME_FAILS:]:
        last_fail[f.tool] = f.detail[:_OUTCOME_DETAIL_CAP]
    for tool, st in tool_stats.items():
        ok = st.get("ok", 0)
        fail = st.get("fail", 0)
        line = f"- {tool}: {ok} ok"
        if fail > 0:
            line += f", {fail} fail"
        if tool in last_fail:
            line += f" — last fail: {last_fail[tool]}"
        lines.append(line)
    return "\n".join(lines[:20])


def _parse_playbook_chunk(content: str) -> tuple[str, str] | None:
    """Parse a playbook entry chunk into (situation, lesson). None if unparseable."""
    situation = None
    lesson_lines: list[str] = []
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("## ") or not s:
            continue
        if s.startswith("[situation]"):
            situation = s[len("[situation]"):].strip()
        elif situation is not None:
            lesson_lines.append(s)
    if situation is None or not lesson_lines:
        return None
    return situation, " ".join(lesson_lines)


async def tool_habits_search(
    memsearch, state: MindState, playbook_path: Path, top_k: int = 2
) -> list[dict]:
    """Retrieve top-k tool lessons relevant to recent tool use + focus.
    Gated: returns [] when there are no tool stats or no playbook file."""
    if not state.tool_stats or not playbook_path.exists():
        return []
    query = " ".join(list(state.tool_stats.keys())[:3])
    if state.focus and state.focus != "idle":
        query += " " + state.focus
    return await memsearch.search(query, top_k=top_k, source_prefix=str(playbook_path.resolve()))


def render_tool_habits(hits: list[dict]) -> str | None:
    """[Tool habits] block from playbook hits, or None when empty/unparseable."""
    if not hits:
        return None
    lines: list[str] = []
    for h in hits:
        parsed = _parse_playbook_chunk(h.get("content", ""))
        if parsed:
            lines.append(f"- [{parsed[0]}] {parsed[1]}")
    if not lines:
        return None
    return "[Tool habits]（過去學到的工具用法）：\n" + "\n".join(lines)


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
