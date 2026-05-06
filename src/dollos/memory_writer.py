"""Memory file writers — transcript and diary helpers.

These helpers append role-tagged turn lines to the daily transcript
markdown and trigger memsearch index_file. Used by:
  - EventDispatcher (user turn) → role="user"
  - Say.run() (Doll turn)        → role="doll"

Transcripts are ephemeral and indexed for same-day recall; they live in
data/memory/transcripts/{date}.md (a separate path from shared LT memory).
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memsearch import MemSearch


async def append_transcript(
    *,
    transcripts_root: Path,
    memsearch: MemSearch,
    role: str,
    text: str,
) -> None:
    """Append a turn line to today's transcript and reindex.

    Format per line: `- [HH:MM <role>] <text>\\n`. role is typically
    "user" or "doll". Caller is responsible for ensuring transcripts_root
    is a directory dedicated to transcripts (separate from shared LT memory).
    """
    path = transcripts_root / f"{date.today():%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%H:%M")
    line = f"- [{timestamp} {role}] {text}\n"
    with path.open("a") as f:
        f.write(line)
    await memsearch.index_file(path)
