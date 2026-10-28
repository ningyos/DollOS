"""[Today's schedule] (plan 2026-10-28 Task 1): a new page's snapshot lists
today's entries that are not yet due, omitted when there are none; a turn's
message resends the block when that list changes. Strangers (external_public)
never see it: it is the owner's day."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time
from pathlib import Path

import pytest

from dollos.mind.mind_prompt import Seen, render_epoch_start, render_turn_delta
from dollos.mind.mind_state import MindState, Perception
from dollos.schedule import Schedule, ScheduleEntry, write_schedule

NOON = datetime(2026, 10, 28, 12, 0, 0).timestamp()


def _schedule(*entries: tuple[str, str]) -> Schedule:
    return Schedule(entries=[
        ScheduleEntry(time=time.fromisoformat(t), intent=intent) for t, intent in entries
    ])


def _block(text: str) -> str:
    start = text.index("[Today's schedule]")
    return text[start:].split("\n\n")[0]


def test_snapshot_lists_only_entries_not_yet_due():
    out = render_epoch_start(MindState(), Seen(), "internal", NOON, schedule=_schedule(
        ("20:00:00", "dinner"), ("09:26:00", "stretch"), ("12:00:00", "lunch"),
        ("15:00:00", "tea"),
    ))
    # Sorted by time; an entry at this very second has arrived.
    assert _block(out).splitlines()[1:] == ["- 15:00:00 — tea", "- 20:00:00 — dinner"]
    assert "stretch" not in out and "lunch" not in out


def test_snapshot_omits_the_block_when_nothing_is_still_to_come():
    for schedule in (None, _schedule(), _schedule(("09:26:00", "stretch"))):
        out = render_epoch_start(MindState(), Seen(), "internal", NOON, schedule=schedule)
        assert "[Today's schedule]" not in out


def test_owner_dm_page_sees_the_schedule():
    out = render_epoch_start(MindState(), Seen(), "external_dm", NOON,
                             schedule=_schedule(("20:00:00", "dinner")))
    assert "- 20:00:00 — dinner" in _block(out)


def test_external_public_never_sees_the_schedule():
    schedule = _schedule(("20:00:00", "dinner with mom"))
    seen = Seen()
    out = render_epoch_start(MindState(), seen, "external_public", NOON, schedule=schedule)
    delta = render_turn_delta(MindState(), seen, "external_public", [], NOON,
                              schedule=_schedule(("21:00:00", "call mom")))
    for text in (out, delta):
        assert "[Today's schedule]" not in text
        assert "mom" not in text


def test_turn_delta_resends_the_block_only_when_it_changes():
    state, seen = MindState(), Seen()
    render_epoch_start(state, seen, "internal", NOON, schedule=None)
    assert "[Today's schedule]" not in render_turn_delta(
        state, seen, "internal", [], NOON, schedule=None)

    schedule = _schedule(("20:00:00", "dinner"))
    delta = render_turn_delta(state, seen, "internal", [], NOON, schedule=schedule)
    assert _block(delta).splitlines()[1:] == ["- 20:00:00 — dinner"]
    assert "[Today's schedule]" not in render_turn_delta(
        state, seen, "internal", [], NOON, schedule=schedule)

    # The entry comes due: the page's block is out of date, so it is resent.
    evening = datetime(2026, 10, 28, 20, 0, 5).timestamp()
    delta = render_turn_delta(state, seen, "internal", [], evening, schedule=schedule)
    assert _block(delta).splitlines()[1:] == ["(none)"]


# ---- MindLoop reads today's file ------------------------------------------- #


def _prompt_text(ml) -> str:
    return "\n".join(m["content"] or "" for m in ml._llm.calls[0]["messages"]
                     if isinstance(m.get("content"), str))


def test_mindloop_snapshot_carries_todays_file_on_owner_pages_only(tmp_path: Path):
    """MindLoop reads ``memory/schedule/<today>.toml`` for the snapshot; an
    owner turn's prompt lists the entry still to come, a stranger's does not."""
    import time as clock

    from tests._mindloop_factory import make_mindloop

    if datetime.now().time() >= time(23, 59):
        pytest.skip("too close to midnight for an entry still to come today")
    write_schedule(tmp_path / "schedule" / f"{date.today():%Y-%m-%d}.toml", [
        ScheduleEntry(time=time(0, 0, 0), intent="already-past-marker"),
        ScheduleEntry(time=time(23, 59, 59), intent="still-to-come-marker"),
    ])

    owner = make_mindloop(memory_root=tmp_path)
    asyncio.run(owner._run_one_turn([Perception(
        kind="UserSpoke", t=clock.time(), data={"text": "hi"},
    )]))
    text = _prompt_text(owner)
    assert "- 23:59:59 — still-to-come-marker" in text
    # The turn's message does not add a second, out-of-date "(none)" block.
    assert text.count("[Today's schedule]") == 1
    assert "already-past-marker" not in text

    stranger = make_mindloop(memory_root=tmp_path)
    asyncio.run(stranger._run_one_turn([Perception(
        kind="ChannelMessage", t=clock.time(),
        data={"content": "hi", "channel_id": "disc:g1:c1",
              "author_is_owner": False, "is_dm": False},
    )]))
    assert stranger._ctx.origin_tier == "external_public"
    assert "still-to-come-marker" not in _prompt_text(stranger)


def _todays_file(memory_root: Path) -> Path:
    return memory_root / "schedule" / f"{date.today():%Y-%m-%d}.toml"


def _owner_says(text: str) -> list[Perception]:
    import time as clock

    return [Perception(kind="UserSpoke", t=clock.time(), data={"text": text})]


@pytest.mark.asyncio
async def test_mindloop_turn_message_carries_a_change_to_todays_file(tmp_path: Path):
    """The block a page opened with goes out of date when the file changes (an
    entry WriteSchedule added, say); the next turn's message on that page
    carries it again."""
    from tests._mindloop_factory import make_mindloop

    if datetime.now().time() >= time(23, 59):
        pytest.skip("too close to midnight for an entry still to come today")
    entry = ScheduleEntry(time=time(23, 59, 59), intent="still-to-come-marker")
    write_schedule(_todays_file(tmp_path), [entry])
    owner = make_mindloop(memory_root=tmp_path)
    await owner._run_one_turn(_owner_says("hi"))

    write_schedule(_todays_file(tmp_path), [
        ScheduleEntry(time=time(23, 59, 58), intent="added-later-marker"), entry,
    ])
    await owner._run_one_turn(_owner_says("and now?"))
    first, second = (c["messages"] for c in owner._llm.calls)
    assert second[1] == first[1]  # the same page
    assert _block(second[-1]["content"]).splitlines()[1:] == [
        "- 23:59:58 — added-later-marker", "- 23:59:59 — still-to-come-marker",
    ]


@pytest.mark.asyncio
async def test_mindloop_diary_request_carries_todays_file(tmp_path: Path):
    import time as clock

    from tests._mindloop_factory import make_mindloop

    if datetime.now().time() >= time(23, 59):
        pytest.skip("too close to midnight for an entry still to come today")
    write_schedule(_todays_file(tmp_path), [
        ScheduleEntry(time=time(23, 59, 59), intent="still-to-come-marker"),
    ])
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn([Perception(kind="DiaryMoment", t=clock.time(), data={})])
    assert ml._moment == "diary"
    request = ml._llm.calls[0]["messages"][1]["content"]
    assert _block(request).splitlines()[1:] == ["- 23:59:59 — still-to-come-marker"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    '[[entry]]\ntime = "8:00:00"\nintent = "one-digit hour"\n',
    '[[entry]]\ntime = "08:00:00"\n',
    '[[entry]]\ntime = "08:00:00"\nintent = "unclosed\n',
])
async def test_mindloop_turn_goes_on_when_todays_file_cannot_be_read(
    tmp_path: Path, body: str, caplog,
):
    """The file is hand-editable (the owner, or Doll through Shell). One that
    cannot be read must not stop every turn until midnight: an owner page
    shows why in the block (and the log has it), a stranger's turn never
    reads the file."""
    import logging

    from tests._mindloop_factory import make_mindloop

    _todays_file(tmp_path).parent.mkdir(parents=True)
    _todays_file(tmp_path).write_text(body)

    owner = make_mindloop(memory_root=tmp_path)
    with caplog.at_level(logging.ERROR, logger="dollos.mind.mind_loop"):
        await owner._run_one_turn(_owner_says("hi"))
    assert len(owner._llm.calls) == 1
    lines = _block(_prompt_text(owner)).splitlines()
    assert len(lines) == 2
    assert lines[1].startswith(f"(cannot read memory/schedule/{_todays_file(tmp_path).name}: ")
    assert any("cannot read" in r.getMessage() for r in caplog.records)

    stranger = make_mindloop(memory_root=tmp_path)
    await stranger._run_one_turn([Perception(
        kind="ChannelMessage", t=datetime.now().timestamp(),
        data={"content": "hi", "channel_id": "disc:g1:c1",
              "author_is_owner": False, "is_dm": False},
    )])
    assert stranger._ctx.origin_tier == "external_public"
    assert len(stranger._llm.calls) == 1
    assert "[Today's schedule]" not in _prompt_text(stranger)
