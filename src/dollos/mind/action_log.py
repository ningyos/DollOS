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


def _count(items, noun: str) -> str:
    n = len(items or [])
    return f"{n} {noun}" + ("" if n == 1 else "s")


def action_phrase_for_tool(name: str, arguments: dict, prior_mood_emotion: str) -> str | None:
    """Her deliberate action → phrase, or None to skip (not whitelisted /
    no material change). Phrases are English and past-tense, written from
    her side with the "I" dropped (`noted: …`, `scheduled 4 things`) —
    LearnName's `someone started calling me "X"` describes something said
    TO her, not an action she performed. Her own content (goal text, note
    text, mood reason) passes through in whatever language she wrote it."""
    a = arguments or {}
    if name == "Shell":
        cmd = redact_secrets(str(a.get("command", ""))).splitlines()[0] if a.get("command") else ""
        return f"ran a command: {_clip(cmd, 80)}"
    if name == "SpawnWorkflow":
        return f"dispatched a workflow ({_count(a.get('tasks'), 'task')})"
    if name == "PursueGoal":
        return f'took on a new goal: "{_clip(a.get("desc", ""), 60)}"'
    if name == "AdvanceGoal":
        return f'advanced goal "{a.get("id", "?")}": {_clip(a.get("progress", ""), 60)}'
    if name == "CloseLoop":
        return f'closed "{a.get("id", "?")}": {_clip(a.get("outcome", ""), 40)}'
    if name == "WriteSchedule":
        done = []
        if a.get("add"):
            done.append(f"scheduled {_count(a.get('add'), 'thing')}")
        if a.get("remove"):
            done.append(f"took {_count(a.get('remove'), 'thing')} off the schedule")
        return " and ".join(done) or None
    if name == "SelfRevision":
        return "adopted a revision of who I am now" if a.get("decision") == "adopt" else None
    if name == "PinSelf":
        return f"updated my self-profile ({a.get('op', '?')} {a.get('section', '?')})"
    if name == "LearnName":
        return f'someone started calling me "{a.get("token", "?")}"' if a.get("op") == "add" else None
    if name == "NoteMemory":
        return f"noted: {_clip(a.get('text', ''), 40)}"
    if name == "MoodTool":
        new = str(a.get("emotion", ""))
        if new and new != prior_mood_emotion:
            reason = _clip(a.get("reason", ""), 40)
            return f'mood changed to "{new}"' + (f": {reason}" if reason else "")
        return None
    return None  # Recall / WriteDiary / Report / Scratchpad / … → skip


def action_phrase_for_perception(kind: str, data: dict) -> str | None:
    """A world event (something that happened to her) → English phrase, or None."""
    d = data or {}
    if kind == "ToolResultArrived":
        tool = d.get("tool", "?")
        task_id = d.get("task_id", "?")
        status = d.get("status", "?")
        summary = _clip(d.get("summary", ""), 80)
        return f'{tool} "{task_id}" finished [{status}]: {summary}'
    if kind in ("BridgeDown", "McpDown"):
        return f"{d.get('service', '?')} went down (rc={d.get('rc', '?')})"
    if kind == "PulseMoment":
        return f"body signal: {_clip(d.get('detail', ''), 80)}"
    return None
