"""Tests for the dollos.schedule module — Phase 1 schedule storage."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from dollos.schedule import (
    Schedule,
    ScheduleEntry,
    due_entries,
    load_schedule,
    write_schedule,
)


def test_load_schedule_parses_toml(tmp_path: Path):
    p = tmp_path / "2026-05-10.toml"
    p.write_text(
        '[[entry]]\n'
        'time = "07:30:00"\n'
        'intent = "morning"\n\n'
        '[[entry]]\n'
        'time = "11:30:00"\n'
        'intent = "lunch ping"\n'
    )
    sched = load_schedule(p)
    assert sched is not None
    assert len(sched.entries) == 2
    assert sched.entries[0].time == time(7, 30)
    assert sched.entries[0].intent == "morning"
    assert sched.entries[1].time == time(11, 30)


def test_load_schedule_missing_file_returns_none(tmp_path: Path):
    assert load_schedule(tmp_path / "nope.toml") is None


def test_load_schedule_accepts_native_time_and_string(tmp_path: Path):
    """gap #6: tomllib parses bare HH:MM:SS as datetime.time; we also accept
    string ISO values written by WriteSchedule."""
    p_str = tmp_path / "string.toml"
    p_str.write_text(
        '[[entry]]\ntime = "08:15:00"\nintent = "x"\n'
    )
    p_native = tmp_path / "native.toml"
    p_native.write_text(
        '[[entry]]\ntime = 08:15:00\nintent = "x"\n'
    )
    s1 = load_schedule(p_str)
    s2 = load_schedule(p_native)
    assert s1 is not None and s2 is not None
    assert s1.entries[0].time == time(8, 15)
    assert s2.entries[0].time == time(8, 15)


@pytest.mark.parametrize(("body", "said"), [
    ('[[entry]]\ntime = "08:00:00"\n', "entry 1"),
    ('[[entry]]\ntime = "08:00:00"\nintent = 5\n', "entry 1"),
    ('[[entry]]\ntime = "08:00:00"\nintent = "a"\n[[entry]]\ntime = "8:00:00"\nintent = "b"\n',
     "entry 2: time must be HH:MM:SS, got '8:00:00'"),
    ('[[entry]]\ntime = 2026-10-28T09:00:00\nintent = "x"\n', "entry 1: time"),
    ('[[entry]]\nintent = "x"\n', "entry 1: time"),
    ("entry = 5\n", "[[entry]]"),
    ('[[entry]]\ntime = 09:00\nintent = "x"\n', "line 2"),  # TOML 1.0 needs the seconds
])
def test_load_schedule_says_what_is_wrong_with_a_hand_edited_file(
    tmp_path: Path, body: str, said: str,
):
    """The file is hand-editable (the owner, or Doll through Shell), and
    every turn reads it (plan 2026-10-28 Task 1). Whatever is wrong with it
    comes out as one ValueError saying what, never a KeyError or a value
    the snapshot cannot sort."""
    p = tmp_path / "sched.toml"
    p.write_text(body)
    with pytest.raises(ValueError, match=re.escape(said)):
        load_schedule(p)


def test_write_schedule_atomic_via_temp_rename(tmp_path: Path):
    p = tmp_path / "sched.toml"
    write_schedule(p, [ScheduleEntry(time=time(7, 30), intent="hi")])
    assert p.exists()
    assert not (tmp_path / "sched.toml.tmp").exists()
    text = p.read_text()
    assert 'time = "07:30:00"' in text
    assert "hi" in text
    # Round-trip via load_schedule.
    sched = load_schedule(p)
    assert sched is not None
    assert sched.entries[0].time == time(7, 30)
    assert sched.entries[0].intent == "hi"


def test_write_schedule_round_trips_any_control_character(tmp_path: Path):
    """Every turn's snapshot reads today's file now (plan 2026-10-28 Task 1),
    so an intent the writer cannot round-trip would fail every turn. TOML
    basic strings take no raw control characters at all."""
    p = tmp_path / "sched.toml"
    intent = 'a\x00b\x0bc\x1bd\x7fe "q" \\ \n\t\r'
    write_schedule(p, [ScheduleEntry(time=time(7, 30), intent=intent)])
    sched = load_schedule(p)
    assert sched is not None
    assert sched.entries[0].intent == intent


def test_due_entries_filters_past_outside_window():
    """gap #7: an entry from earlier in the day (more than 1 minute ago)
    is NOT fired retroactively — only entries within the last minute fire."""
    now = datetime(2026, 5, 10, 14, 0, 0)
    sched = Schedule(entries=[
        ScheduleEntry(time=time(8, 0), intent="too old"),
        ScheduleEntry(time=time(13, 59, 30), intent="recent"),
    ])
    out = due_entries(sched, now, set())
    assert [e.intent for e in out] == ["recent"]


def test_due_entries_includes_recent_due():
    now = datetime(2026, 5, 10, 14, 0, 5)
    sched = Schedule(entries=[
        ScheduleEntry(time=time(14, 0), intent="now"),
        ScheduleEntry(time=time(15, 0), intent="future"),
    ])
    out = due_entries(sched, now, set())
    assert [e.intent for e in out] == ["now"]


def test_due_entries_excludes_already_fired():
    now = datetime(2026, 5, 10, 14, 0, 5)
    sched = Schedule(entries=[ScheduleEntry(time=time(14, 0), intent="now")])
    out = due_entries(sched, now, {time(14, 0)})
    assert out == []
