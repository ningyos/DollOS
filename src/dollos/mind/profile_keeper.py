"""Daily profile keeper (remember-corrections spec §2.2, plan Task 4).

After the day's consolidation, one keeper agent reads that day's transcript
and the owner sections of self_profile (user / relationship, with ids) and
writes in what the owner corrected or updated that day and Doll did not pin
herself: ``EditOwnerProfile`` add / replace / remove, each change tied to a
verbatim quote of the owner. Most days it changes nothing and just Reports.

The quote check (decision 4) lives here: plain string matching against the
owner lines of the transcript file on disk, never a model's judgment.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from dollos.agent_engine import run_agent
from dollos.memory_writer import is_action_log_line
from dollos.mind import self_profile
from dollos.tools import PROFILE_KEEPER_TOOLS

logger = logging.getLogger(__name__)

# Decision 4: a shorter quote matches almost any line, so it proves nothing.
MIN_QUOTE_CHARS = 4

# A transcript entry starts with ``- HH:MM:SS `` (memory_writer.
# append_transcript / append_action_log); a line without it continues the
# entry above (a message with a line break in it).
_ENTRY_RE = re.compile(r"^- \d{2}:\d{2}:\d{2} ")
_OWNER = "Owner: "


def _normalize(text: str) -> str:
    """Strip and collapse every whitespace run to one space (decision 4)."""
    return " ".join(text.split())


def owner_lines(transcript: str) -> list[str]:
    """The owner's words in a day's transcript, one normalized string per
    entry. ``Me:`` entries and action-log entries never count: only what
    the owner said can back a change to what Doll knows about them."""
    entries: list[str] = []
    for line in transcript.splitlines():
        if _ENTRY_RE.match(line):
            entries.append(line)
        elif entries:
            entries[-1] += "\n" + line
    out: list[str] = []
    for entry in entries:
        if is_action_log_line(entry):
            continue
        body = _ENTRY_RE.sub("", entry, count=1)
        if body.startswith(_OWNER):
            out.append(_normalize(body[len(_OWNER):]))
    return out


def owner_said(transcript_path: Path, quote: str) -> bool:
    """True iff the normalized ``quote`` (at least ``MIN_QUOTE_CHARS``) is a
    substring of one owner entry in the transcript at ``transcript_path``."""
    q = _normalize(quote)
    if len(q) < MIN_QUOTE_CHARS:
        return False
    text = transcript_path.read_text(encoding="utf-8")
    return any(q in line for line in owner_lines(text))


_PROFILE_KEEPER_TASK = """You keep the part of your self-profile that is about your owner: the user section (what you've noticed about your owner) and the relationship section (you and your owner). Below are their current entries and today's transcript of you and your owner.

Look only for what your owner corrected or updated today about lasting facts about them, or about how they want to be treated. For each such correction, use EditOwnerProfile: replace the entry it contradicts, remove an entry that is no longer true, or add a new entry when none covers it. Write each entry as a plain fact about your owner or the two of you, not as a story of being corrected. Leave out one-off things such as appointments and reminders. Every change needs quote: your owner's own words from today's transcript, copied exactly; your own lines do not count. If the profile is full, remove outdated entries first, then add. The self section is not part of this task.

Most days there is nothing to change; then just call Report. When you are done, call Report with what you changed.

Current entries:
{entries}

Today's transcript:
{transcript}
"""


def _owner_entries(path: Path) -> str:
    sections = self_profile.read_sections(path)
    out: list[str] = []
    for key in ("user", "relationship"):
        out.append(f"## {key}: {self_profile.SECTION_TITLES[key]}")
        out.extend(f"- {b.id}: {b.text}" for b in sections[key])
        if not sections[key]:
            out.append("(none yet)")
    return "\n".join(out)


async def run_profile_keeper(
    *,
    target_date: str,
    model,
    renderer,
    memsearch,
    memory_root: Path,
    transcripts_root: Path,
    tool_output_store,
    max_tokens: int = 2048,
    think_budget_tokens: int = 1024,
    transcript_tail_chars: int = 8000,
    self_profile_max_chars: int = 4000,
) -> bool:
    """Run the profile keeper over ``target_date``'s transcript.

    Returns True when the keeper ran to its Report (changed something or
    not), False when there is no transcript or it never Reported. Raises
    CancelledError through (the caller treats it as a cancel).
    """
    src = transcripts_root / f"{target_date}.md"
    if not src.exists():
        logger.info("profile keeper: no transcript for %s; skip", target_date)
        return False
    raw = src.read_text(encoding="utf-8")
    # Same cut as consolidation (spec §2.2): action-log lines are not the
    # conversation, so they neither show nor starve the tail budget.
    convo = "\n".join(ln for ln in raw.splitlines() if not is_action_log_line(ln))
    transcript = convo[-transcript_tail_chars:]

    task = _PROFILE_KEEPER_TASK.format(
        entries=_owner_entries(memory_root / "self_profile.md"),
        transcript=transcript,
    )
    report = await run_agent(
        task=task,
        system=renderer.render("subagent_scaffolding"),
        model=model,
        renderer=renderer,
        memory_root=memory_root,
        memsearch=memsearch,
        transcripts_root=transcripts_root,
        tool_output_store=tool_output_store,
        tools=PROFILE_KEEPER_TOOLS, kind="keeper",
        max_tokens=max_tokens,
        think_budget=think_budget_tokens,
        purpose="profile_keeper",
        shell_runner=None,
        # The quote check reads the whole day's file, not the tail the
        # keeper was shown: the owner said it today either way.
        owner_transcript=src,
        self_profile_max_chars=self_profile_max_chars,
    )
    if report is None:
        logger.warning("profile keeper: no report for %s", target_date)
        return False
    logger.info("profile keeper: done for %s: %s", target_date, report.get("summary", ""))
    return True
