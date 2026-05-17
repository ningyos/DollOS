"""MonitorRunner unit tests — spawn, line firing, regex, rate-limit, exit, remove."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dollos.mind.mind_state import Perception
from dollos.mind.perception_queue import PerceptionQueue
from dollos.monitor_runner import MonitorRunner


def _make_runner(tmp_path: Path) -> tuple[MonitorRunner, PerceptionQueue]:
    queue = PerceptionQueue()
    runner = MonitorRunner(cwd=tmp_path, perception_queue=queue)
    return runner, queue


async def _wait_for_perception_kind(
    queue: PerceptionQueue, kinds: set[str], timeout: float = 5.0
) -> list[Perception]:
    """Collect perceptions until we have at least one of the given kinds."""
    collected: list[Perception] = []
    for _ in range(int(timeout / 0.05)):
        batch = await queue.drain(timeout_s=0.05)
        collected.extend(batch)
        if any(p.kind in kinds for p in collected):
            # Collect a bit more to get all rapid-fire ones
            await asyncio.sleep(0.1)
            batch2 = await queue.drain(timeout_s=0.05)
            collected.extend(batch2)
            return collected
    return collected


def _triggers(perceptions: list[Perception]) -> list[Perception]:
    return [p for p in perceptions if p.kind == "MonitorFired"]


def _exits(perceptions: list[Perception]) -> list[Perception]:
    return [p for p in perceptions if p.kind == "MonitorEnded"]


@pytest.mark.asyncio
async def test_monitor_runner_fires_per_line(tmp_path: Path) -> None:
    runner, queue = _make_runner(tmp_path)
    mon_id = runner.spawn(
        command="printf 'a\\nb\\nc\\n'",
        match_regex=None,
        rate_limit_s=0,
    )
    assert mon_id == "mon-1"
    perceptions = await _wait_for_perception_kind(queue, {"MonitorEnded"})
    trig = _triggers(perceptions)
    assert [p.data["line"] for p in trig] == ["a", "b", "c"]
    assert all(p.data["suppressed_count"] == 0 for p in trig)
    exited = _exits(perceptions)
    assert len(exited) == 1
    assert exited[0].data["exit_status"] == "natural"
    assert exited[0].data["exit_code"] == 0
    assert exited[0].data["total_matched"] == 3
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_regex_filters_lines(tmp_path: Path) -> None:
    runner, queue = _make_runner(tmp_path)
    runner.spawn(
        command="printf '85\\n70\\n91\\n65\\n'",
        match_regex=r"^[89][0-9]$",
        rate_limit_s=0,
    )
    perceptions = await _wait_for_perception_kind(queue, {"MonitorEnded"})
    trig = _triggers(perceptions)
    assert [p.data["line"] for p in trig] == ["85", "91"]
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_rate_limit_suppresses_extra_lines(tmp_path: Path) -> None:
    """With rate_limit_s very high, only the first match in the window fires."""
    runner, queue = _make_runner(tmp_path)
    runner.spawn(
        command="printf 'hit\\nhit\\nhit\\nhit\\n'",
        match_regex=None,
        rate_limit_s=60,
    )
    perceptions = await _wait_for_perception_kind(queue, {"MonitorEnded"})
    trig = _triggers(perceptions)
    # First line fires, the next 3 are suppressed.
    assert len(trig) == 1
    assert trig[0].data["line"] == "hit"
    assert trig[0].data["suppressed_count"] == 0  # first fire — no prior suppression
    exited = _exits(perceptions)
    assert exited[0].data["total_matched"] == 4
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_active_state_reports_suppression(tmp_path: Path) -> None:
    """active_state() exposes suppressed_in_window for [Active monitors] block."""
    runner, queue = _make_runner(tmp_path)
    # Long-running command so monitor stays active during inspection.
    runner.spawn(
        command="bash -c 'for i in 1 2 3 4 5; do echo hit; sleep 0.05; done; sleep 10'",
        match_regex=None,
        rate_limit_s=60,
    )
    # Wait until we've emitted some lines AND at least one fire happened.
    perceptions = await _wait_for_perception_kind(queue, {"MonitorFired"})
    assert _triggers(perceptions)
    await asyncio.sleep(0.4)  # let the loop finish all 5 prints
    state = runner.active_state()
    assert len(state) == 1
    snap = state[0]
    assert snap["monitor_id"] == "mon-1"
    assert snap["rate_limit_s"] == 60
    assert snap["suppressed_in_window"] >= 1  # 4 lines beyond the first fire
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_remove_kills_proc_and_fires_exit(tmp_path: Path) -> None:
    runner, queue = _make_runner(tmp_path)
    mon_id = runner.spawn(
        command="sleep 30",
        match_regex=None,
        rate_limit_s=0,
    )
    await asyncio.sleep(0.2)
    removed = await runner.remove(mon_id)
    assert removed is True
    perceptions = await _wait_for_perception_kind(queue, {"MonitorEnded"})
    exited = _exits(perceptions)
    assert len(exited) == 1
    assert exited[0].data["exit_status"] == "removed"
    assert mon_id not in {s["monitor_id"] for s in runner.active_state()}
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_remove_unknown_returns_false(tmp_path: Path) -> None:
    runner, queue = _make_runner(tmp_path)
    removed = await runner.remove("mon-999")
    assert removed is False
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_stop_kills_all_active(tmp_path: Path) -> None:
    runner, queue = _make_runner(tmp_path)
    runner.spawn(command="sleep 30", match_regex=None, rate_limit_s=0)
    runner.spawn(command="sleep 30", match_regex=None, rate_limit_s=0)
    await asyncio.sleep(0.1)
    assert len(runner.active_state()) == 2
    await runner.stop()
    assert runner.active_state() == []
