"""Daily schedule storage + loader.

Phase 1 schedule foundation: Doll plans her day via the WriteSchedule tool,
which persists into ``data/memory/schedule/{YYYY-MM-DD}.toml``. The kernel's
``_schedule_runner`` polls this file every 30s and fires ``ScheduledEvent``
entries via the dispatcher when their time arrives; MindLoop reads it for
the ``[Today's schedule]`` snapshot block.

This module deliberately stays small + pure — ``due_entries`` is a plain
function that takes ``now`` + ``fired`` so unit tests do not need to mock
the clock.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

# Control characters a TOML basic string may not hold raw.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class ScheduleEntry:
    time: time
    intent: str


@dataclass
class Schedule:
    entries: list[ScheduleEntry]


def load_schedule(path: Path) -> Schedule | None:
    """Returns None if file missing.

    Accepts both TOML native local-time values (parsed by ``tomllib`` into a
    ``datetime.time``) and string ISO ``HH:MM:SS`` values for the ``time``
    field (gap #6 — WriteSchedule writes its values as quoted strings inside
    the TOML so we round-trip through string form on read).

    The file is hand-editable (the owner, or Doll through Shell), so anything
    else in it raises ValueError saying what is wrong (tomllib's own
    TOMLDecodeError is one): a loaded Schedule only ever holds times and
    string intents.
    """
    if not path.exists():
        return None
    with path.open("rb") as f:
        data = tomllib.load(f)
    raw_entries = data.get("entry", [])
    if not isinstance(raw_entries, list):
        raise ValueError("entries must be [[entry]] tables")
    entries: list[ScheduleEntry] = []
    for n, raw in enumerate(raw_entries, 1):
        if not isinstance(raw, dict) or not isinstance(raw.get("intent"), str):
            raise ValueError(f"entry {n} has no intent string")
        given = t = raw.get("time")
        if isinstance(t, str):
            try:
                t = datetime.fromisoformat(f"1970-01-01T{t}").time()
            except ValueError:
                t = None
        if not isinstance(t, time):
            raise ValueError(f"entry {n}: time must be HH:MM:SS, got {given!r}")
        entries.append(ScheduleEntry(time=t, intent=raw["intent"]))
    return Schedule(entries=entries)


def write_schedule(path: Path, entries: list[ScheduleEntry]) -> None:
    """Atomic write via temp + rename.

    The ``time`` field is written as a quoted string (``HH:MM:SS``) so the
    on-disk format does not depend on tomllib's local-time literal support.
    ``intent`` uses a triple-quoted block so newlines / quotes inside the
    intent do not break the TOML.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        f.write(f"# {date.today().isoformat()}\n\n")
        for e in entries:
            f.write("[[entry]]\n")
            f.write(f'time = "{e.time:%H:%M:%S}"\n')
            # Single-line basic TOML string with backslash + double-quote
            # escaping. Embedded newlines become \n escape sequences so the
            # value round-trips cleanly through tomllib; any other control
            # character (TOML takes none raw) becomes a \uXXXX escape.
            escaped = (
                e.intent.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t")
            )
            escaped = _CONTROL_RE.sub(lambda m: f"\\u{ord(m.group()):04x}", escaped)
            f.write(f'intent = "{escaped}"\n\n')
    tmp.rename(path)


def due_entries(
    schedule: Schedule, now: datetime, fired: set[time]
) -> list[ScheduleEntry]:
    """Pure function — entries within a 1-minute window of ``now``.

    An entry is due if all hold:
      * its ``time`` is not already in ``fired``;
      * ``now.time() >= entry.time`` (entry's moment has arrived);
      * ``entry.time`` was within the last minute (gap #7 — past entries
        from earlier in the day are SKIPPED, e.g. when the daemon was
        offline through them).
    """
    out: list[ScheduleEntry] = []
    one_min_ago = now - timedelta(minutes=1)
    for e in schedule.entries:
        if e.time in fired:
            continue
        moment = datetime.combine(now.date(), e.time)
        if moment > now:
            continue
        if moment < one_min_ago:
            continue
        out.append(e)
    return out
