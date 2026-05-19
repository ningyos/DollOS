"""Render MindState into a complete LLM prompt for one MindLoop iteration.

Returns: system_prompt + 10 dynamic blocks as a single string.
"""
from __future__ import annotations

import time

from dollos.mind.mind_state import MindState


def render_mind(
    state: MindState,
    memsearch_hits: list[dict],
    system_prompt: str,
    *,
    pulse_block: str | None = None,
    cognition_block: str | None = None,
    associative_hits: list[dict] | None = None,
) -> str:
    """Compose: system_prompt + 10 dynamic blocks.

    ``pulse_block`` — optional pre-rendered ``[Self pulse]`` block from
    ``perception.system_pulse.SystemPulse.snapshot()``. Inserted after
    ``[Active tasks]`` (mirrors the ``[Active monitors]`` slot).

    ``cognition_block`` — optional pre-rendered ``[Cognition]`` block from
    ``perception.cognition.CognitionWorker.snapshot()``. Inserted right
    after ``[Self pulse]``.
    """
    now = time.time()
    blocks = [
        system_prompt,
        "",
        "[Memory context]",
        _render_memory(memsearch_hits),
        "",
        "[Associative memories]",
        _render_associative(associative_hits or []),
        "",
        "[Mind state]",
        _render_mindstate(state, now),
        "",
        "[Active tasks] (currently running, you cannot cancel)",
        _render_active_tasks(state.active_tasks, now),
        "",
    ]
    if pulse_block:
        blocks.extend([pulse_block, ""])
    if cognition_block:
        blocks.extend([cognition_block, ""])
    blocks.extend([
        "[Open loops] (commitments you have made)",
        _render_open_loops(state.open_loops, now),
        "",
        "[Pending] (upcoming scheduled events)",
        _render_pending(state.pending_events, now),
        "",
        "[Scratchpad]",
        state.scratchpad or "(empty)",
        "",
        "[Recent perceptions] (newest last)",
        _render_perceptions(state.recent_perceptions, now),
        "",
        _render_outputs_header(state.recent_outputs, now),
        _render_outputs(state.recent_outputs, now),
        "",
        "[Decision time]",
        "What do you do this iteration? Output a JSON array of 0..N actions.",
    ])
    return "\n".join(blocks)


def _render_memory(hits: list[dict]) -> str:
    if not hits:
        return "(no relevant memories)"
    return "\n".join(f"- {h.get('content', str(h))}" for h in hits)


def _render_associative(hits: list[dict]) -> str:
    """Render context-associative recall hits.

    Each bullet: ``- [axis=value] <snippet (100 chars)>``. Hits without
    an ``_axis`` marker are skipped (they wouldn't be here in practice).
    Capped to 6 bullets for token cost.
    """
    if not hits:
        return "(none)"
    lines: list[str] = []
    for h in hits[:6]:
        axis = h.get("_axis")
        value = h.get("_axis_value", "")
        snippet = (h.get("content") or "").strip().replace("\n", " ")
        if len(snippet) > 100:
            snippet = snippet[:97] + "..."
        tag = f"{axis}={value}" if axis else "?"
        lines.append(f"- [{tag}] {snippet}")
    return "\n".join(lines)


def _render_mindstate(state: MindState, now: float) -> str:
    mood_str = state.mood.emotion
    if state.mood.reason:
        mood_str = f"{state.mood.emotion} ({state.mood.reason})"
    last_user = _human_secs(now - state.last_user_at) + " ago" if state.last_user_at else "never"
    return (
        f"focus: {state.focus}\n"
        f"mood: {mood_str}\n"
        f"last_user: {last_user}\n"
        f"iter: {state.iter_count}"
    )


def _render_active_tasks(tasks: list, now: float) -> str:
    if not tasks:
        return "(none)"
    return "\n".join(
        f"- {t.task_id}: {t.summary}, elapsed {_human_secs(now - t.started_at)}"
        for t in tasks
    )


def _render_open_loops(loops: list, now: float) -> str:
    if not loops:
        return "(none)"
    return "\n".join(
        f"- {lp.id}: {lp.desc} (opened {_human_secs(now - lp.opened_at)} ago)"
        for lp in loops
    )


def _render_pending(events: list, now: float) -> str:
    if not events:
        return "(none)"
    return "\n".join(
        f"- in {_human_secs(e.fire_at - now)}: {e.summary}"
        for e in events
    )


def _render_perceptions(percs, now: float) -> str:
    if not percs:
        return "(none)"
    out = []
    for p in percs:
        age = _human_secs(now - p.t)
        body = _percep_body(p)
        out.append(f"[{age} ago] {p.kind}: {body}")
    return "\n".join(out)


def _percep_body(p) -> str:
    d = p.data or {}
    if p.kind == "UserSpoke":
        return f"'{d.get('text', '')[:200]}'"
    if p.kind == "ToolResultArrived":
        return f"{d.get('tool', '?')} {d.get('task_id', '?')}: {d.get('summary', '')[:120]}"
    if p.kind == "MonitorFired":
        return f"{d.get('monitor_id', '?')}: {d.get('line', '')[:120]}"
    if p.kind == "MonitorEnded":
        return f"{d.get('monitor_id', '?')} (exit {d.get('exit_status', '?')})"
    if p.kind == "ScheduledMoment":
        return d.get("text", "")[:200]
    if p.kind == "Awoke":
        return f"reason={d.get('reason', '?')}"
    if p.kind == "ReflectionMoment":
        return f"(time to reflect — review recent activity and NoteMemory anything worth keeping; {d.get('iters_since_last', '?')} iters since last)"
    if p.kind == "Interrupted":
        by = d.get("by", "user")
        return f"your previous turn was cut short by {by}"
    return str(d)[:120]


def _render_outputs_header(outs, now: float) -> str:
    """Render the [Recent outputs] section header.

    If the most recent output was a Speech within the last 30 seconds, inject an
    inline WARNING nudge so the model has a concrete signal not to speak again.
    """
    base = "[Recent outputs] (what you did recently — avoid repeating the same content)"
    if not outs:
        return base
    last = list(outs)[-1]
    if last.kind == "Speech":
        age_s = now - last.t
        if age_s < 30:
            snippet = last.summary[len("spoke: "):60] if last.summary.startswith("spoke: ") else last.summary[:60]
            return (
                f"{base}\n"
                f"⚠ WARNING: you just spoke {age_s:.0f}s ago ('{snippet}…'). "
                f"Don't repeat yourself — if there's nothing new to say, stay silent (no text outside tool_call)."
            )
    return base


def _render_outputs(outs, now: float) -> str:
    if not outs:
        return "(none)"
    return "\n".join(
        f"[{_human_secs(now - o.t)} ago] {o.summary}"
        for o in outs
    )


def _human_secs(s: float) -> str:
    """Format a seconds count as 'Ns', 'Nm', or 'Nh' depending on magnitude."""
    if s < 0:
        s = 0.0
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.0f}m"
    return f"{s / 3600:.1f}h"
