"""Memory file writers — transcript and diary helpers.

These helpers append role-tagged turn lines to the daily transcript
markdown and trigger memsearch index_file. Used by:
  - EventDispatcher (user turn) → role="user"
  - mind_loop (Doll turn naked text segments) → role="doll"

Transcripts are ephemeral and indexed for same-day recall; they live in
data/memory/transcripts/{date}.md (a separate path from shared LT memory).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dollos.memory import FtsMemory


ACTION_PREFIX = "▸"
_ACTION_LINE_RE = re.compile(rf"^- \d{{2}}:\d{{2}}:\d{{2}} {re.escape(ACTION_PREFIX)} ")


async def append_transcript(
    *,
    transcripts_root: Path,
    memsearch: FtsMemory,
    role: str,
    text: str,
) -> None:
    """Append a turn line to today's transcript and reindex.

    Format per line: `- [HH:MM:SS <role>] <text>\\n`. role is typically
    "user" or "doll". Caller is responsible for ensuring transcripts_root
    is a directory dedicated to transcripts (separate from shared LT memory).
    """
    path = transcripts_root / f"{date.today():%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%H:%M:%S")
    if role == "user":
        speaker = "主人"
    elif role == "doll":
        speaker = "我"
    else:
        speaker = role  # fallback for any future roles
    line = f"- {timestamp} {speaker}說：{text}\n"
    with path.open("a") as f:
        f.write(line)
    await memsearch.index_file(path)


def is_action_log_line(line: str) -> bool:
    """True iff `line` is an action/event log line (▸-prefixed), not a
    conversation turn line written by append_transcript."""
    return bool(_ACTION_LINE_RE.match(line))


async def append_action_log(
    *,
    transcripts_root: Path,
    memsearch: FtsMemory,
    phrase: str,
) -> None:
    """Append one ▸-marked action/event line to today's transcript and reindex.

    Format: `- HH:MM:SS ▸ <phrase>\\n`. Shares the daily transcript file with
    append_transcript so the diary reads one coherent day; the ▸ prefix lets
    consolidation filter these out (is_action_log_line).

    Whole-branch review WB-2 (I2 invariant chokepoint): some mapper fields
    (MoodTool emotion, LearnName token, PinSelf op/section, AdvanceGoal/
    CloseLoop id) are interpolated into `phrase` without `_clip`, so a
    model-emitted newline could split the action line — the tail then loses
    its ▸ prefix, `is_action_log_line` stops matching it, and it leaks past
    consolidation's action-log filter. Sanitized HERE, at the single writer
    chokepoint, rather than at every call site — collapsing any embedded
    newline to a space guarantees every action write is exactly one line,
    regardless of which mapper produced `phrase`."""
    phrase = phrase.replace("\n", " ").replace("\r", " ")
    path = transcripts_root / f"{date.today():%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"- {timestamp} {ACTION_PREFIX} {phrase}\n"
    with path.open("a") as f:
        f.write(line)
    await memsearch.index_file(path)
