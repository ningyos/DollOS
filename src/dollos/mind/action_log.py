"""Diary action-log: map a (tool, args) or (perception) into an optional
one-line past-tense phrase for the day's action log. Pure functions — the
whitelist + summaries + secret redaction live here; wiring lives in mind_loop.
"""
from __future__ import annotations

import re

_SECRET_RE = re.compile(
    r"(?i)\b(token|password|passwd|pwd|secret|api[_-]?key|authorization|bearer)"
    r"(\s*[:=]\s*|\s+)(?:Bearer\s+)?([^\s&|;]+)"
)


def redact_secrets(cmd: str) -> str:
    """Best-effort: replace the value after a secret-ish key with ***."""
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", cmd)


def _clip(s: str, n: int) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n]


def action_phrase_for_tool(name: str, arguments: dict, prior_mood_emotion: str) -> str | None:
    """Her deliberate action → phrase, or None to skip (not whitelisted /
    no material change). Phrases are past-tense; most start with 我 (her
    doing it) — e.g. LearnName's 有人開始叫我「X」 does not, since that one
    describes something said TO her, not an action she performed."""
    a = arguments or {}
    if name == "Shell":
        cmd = redact_secrets(str(a.get("command", ""))).splitlines()[0] if a.get("command") else ""
        return f"我跑了指令 {_clip(cmd, 80)}"
    if name == "SpawnWorkflow":
        return f"我派了 workflow({len(a.get('tasks') or [])} 個工作)"
    if name == "SpawnMonitor":
        return f"我設了 monitor:{_clip(a.get('command', ''), 60)}"
    if name == "RemoveMonitor":
        return f"我撤了 monitor {a.get('monitor_id', '?')}"
    if name == "PursueGoal":
        return f"我起了新目標:「{_clip(a.get('desc', ''), 60)}」"
    if name == "AdvanceGoal":
        return f"我推進了目標「{a.get('id', '?')}」:{_clip(a.get('progress', ''), 60)}"
    if name == "CloseLoop":
        return f"我收掉了「{a.get('id', '?')}」:{_clip(a.get('outcome', ''), 40)}"
    if name == "WriteSchedule":
        return f"我替未來排了 {len(a.get('entries') or [])} 件事"
    if name == "SelfRevision":
        return "我採納了對自我的修訂" if a.get("decision") == "adopt" else None
    if name == "PinSelf":
        return f"我整理了自我({a.get('op', '?')} {a.get('section', '?')})"
    if name == "LearnName":
        return f"有人開始叫我「{a.get('token', '?')}」" if a.get("op") == "add" else None
    if name == "NoteMemory":
        return f"我記下了:{_clip(a.get('text', ''), 40)}"
    if name == "MoodTool":
        new = str(a.get("emotion", ""))
        if new and new != prior_mood_emotion:
            reason = _clip(a.get("reason", ""), 40)
            return f"我心情變成「{new}」" + (f":{reason}" if reason else "")
        return None
    return None  # Recall / WriteDiary / Report / Scratchpad / … → skip


def action_phrase_for_perception(kind: str, data: dict) -> str | None:
    """A world event (something that happened to her) → phrase, or None."""
    d = data or {}
    if kind == "ToolResultArrived":
        tool = d.get("tool", "?")
        task_id = d.get("task_id", "?")
        status = d.get("status", "?")
        summary = _clip(d.get("summary", ""), 80)
        return f"{tool}「{task_id}」跑完了[{status}]:{summary}"
    if kind == "MonitorFired":
        return f"Monitor {d.get('monitor_id', '?')} 觸發:{_clip(d.get('line', ''), 80)}"
    if kind == "MonitorEnded":
        return f"Monitor {d.get('monitor_id', '?')} 結束(exit {d.get('exit_status', '?')})"
    if kind in ("BridgeDown", "McpDown"):
        return f"{d.get('service', '?')} 掛了(rc={d.get('rc', '?')})"
    return None
