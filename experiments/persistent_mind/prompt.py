"""Render the full mind-state prompt."""
from __future__ import annotations

import time

from mind_state import MindState


def _fmt_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}m"
    return f"{seconds/3600:.1f}h"


def render_mind_prompt(state: MindState) -> str:
    now = time.time()
    session_age = now - state.session_started_at if state.session_started_at else 0

    lines: list[str] = []
    lines.append("[Mind state]")
    lines.append(f"focus: {state.focus}")
    lines.append(f"mood: {state.mood}")
    lines.append(f"energy: {state.energy:.2f}")
    lines.append(f"session_age: {_fmt_time(session_age)}")
    lines.append(f"iter: {state.iter_count}")
    if state.last_user_at:
        lines.append(f"last_user: {_fmt_time(now - state.last_user_at)} ago")
    lines.append("")

    lines.append("[Active tasks] (currently running, you cannot cancel — wait for result)")
    if state.active_tasks:
        for t in state.active_tasks:
            elapsed = now - t.get("started_at", now)
            lines.append(
                f"- id={t.get('id','?')} tool={t.get('tool','?')} "
                f"cmd={t.get('command','')!r} elapsed={_fmt_time(elapsed)} "
                f"timeout={t.get('timeout_s','?')}s"
            )
    else:
        lines.append("(none)")
    lines.append("")

    lines.append("[Open loops] (commitments you have made to follow up)")
    if state.open_loops:
        for l in state.open_loops:
            age = now - l.get("opened_at", now)
            lines.append(f"- id={l['id']} desc={l['desc']!r} (open {_fmt_time(age)})")
    else:
        lines.append("(none)")
    lines.append("")

    lines.append("[Recent perceptions] (newest last)")
    if state.recent_perceptions:
        for p in state.recent_perceptions:
            lines.append(p.render())
    else:
        lines.append("(none)")
    lines.append("")

    lines.append("[Recent thoughts]")
    if state.recent_thoughts:
        for t in state.recent_thoughts:
            lines.append(f"- {t}")
    else:
        lines.append("(none)")
    lines.append("")

    if state.scratchpad:
        lines.append("[Scratchpad]")
        lines.append(state.scratchpad)
        lines.append("")

    lines.append("[Decision time]")
    lines.append(
        "What do you do this iteration? Output one or more actions as a JSON array. "
        "If nothing to do, output [{\"action\":\"Idle\"}]."
    )
    return "\n".join(lines)
