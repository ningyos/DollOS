"""MonitorRunner unit tests — spawn, line firing, regex, rate-limit, exit, remove."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from dollos.events import (
    MonitorExitedEvent,
    MonitorTriggeredEvent,
    RawEvent,
)
from dollos.monitor_runner import MonitorRunner


class _Capture:
    def __init__(self) -> None:
        self.events: list[RawEvent] = []

    def __call__(self, ev: RawEvent) -> None:
        self.events.append(ev)


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    """Spin until predicate() is truthy or timeout."""
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {predicate}")


@pytest.mark.asyncio
async def test_monitor_runner_fires_per_line(tmp_path: Path) -> None:
    cap = _Capture()
    runner = MonitorRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    mon_id = runner.spawn(
        command="printf 'a\\nb\\nc\\n'",
        match_regex=None,
        rate_limit_s=0,
        response_sink=sink,
    )
    assert mon_id == "mon-1"
    await _wait_for(
        lambda: any(isinstance(e, MonitorExitedEvent) for e in cap.events)
    )
    triggers = [e for e in cap.events if isinstance(e, MonitorTriggeredEvent)]
    assert [t.line for t in triggers] == ["a", "b", "c"]
    assert all(t.suppressed_count == 0 for t in triggers)
    exit_ev = [e for e in cap.events if isinstance(e, MonitorExitedEvent)][0]
    assert exit_ev.status == "natural"
    assert exit_ev.exit_code == 0
    assert exit_ev.total_matched == 3
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_regex_filters_lines(tmp_path: Path) -> None:
    cap = _Capture()
    runner = MonitorRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(
        command="printf '85\\n70\\n91\\n65\\n'",
        match_regex=r"^[89][0-9]$",
        rate_limit_s=0,
        response_sink=sink,
    )
    await _wait_for(
        lambda: any(isinstance(e, MonitorExitedEvent) for e in cap.events)
    )
    triggers = [e for e in cap.events if isinstance(e, MonitorTriggeredEvent)]
    assert [t.line for t in triggers] == ["85", "91"]
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_rate_limit_suppresses_extra_lines(
    tmp_path: Path,
) -> None:
    """With rate_limit_s very high, only the first match in the window fires."""
    cap = _Capture()
    runner = MonitorRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(
        command="printf 'hit\\nhit\\nhit\\nhit\\n'",
        match_regex=None,
        rate_limit_s=60,
        response_sink=sink,
    )
    await _wait_for(
        lambda: any(isinstance(e, MonitorExitedEvent) for e in cap.events)
    )
    triggers = [e for e in cap.events if isinstance(e, MonitorTriggeredEvent)]
    # First line fires, the next 3 are suppressed.
    assert len(triggers) == 1
    assert triggers[0].line == "hit"
    assert triggers[0].suppressed_count == 0  # first fire — no prior suppression
    exit_ev = [e for e in cap.events if isinstance(e, MonitorExitedEvent)][0]
    assert exit_ev.total_matched == 4
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_active_state_reports_suppression(
    tmp_path: Path,
) -> None:
    """active_state() exposes suppressed_in_window for [Active monitors] block."""
    cap = _Capture()
    runner = MonitorRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    # Long-running command so monitor stays active during inspection.
    runner.spawn(
        command="bash -c 'for i in 1 2 3 4 5; do echo hit; sleep 0.05; done; sleep 10'",
        match_regex=None,
        rate_limit_s=60,
        response_sink=sink,
    )
    # Wait until we've emitted some lines AND at least one fire happened.
    await _wait_for(
        lambda: any(isinstance(e, MonitorTriggeredEvent) for e in cap.events)
    )
    await asyncio.sleep(0.4)  # let the loop finish all 5 prints
    state = runner.active_state()
    assert len(state) == 1
    snap = state[0]
    assert snap["monitor_id"] == "mon-1"
    assert snap["rate_limit_s"] == 60
    assert snap["suppressed_in_window"] >= 1  # 4 lines beyond the first fire
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_remove_kills_proc_and_fires_exit(
    tmp_path: Path,
) -> None:
    cap = _Capture()
    runner = MonitorRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    mon_id = runner.spawn(
        command="sleep 30",
        match_regex=None,
        rate_limit_s=0,
        response_sink=sink,
    )
    await asyncio.sleep(0.2)
    removed = await runner.remove(mon_id)
    assert removed is True
    await _wait_for(
        lambda: any(isinstance(e, MonitorExitedEvent) for e in cap.events)
    )
    exit_ev = [e for e in cap.events if isinstance(e, MonitorExitedEvent)][0]
    assert exit_ev.status == "removed"
    assert mon_id not in {s["monitor_id"] for s in runner.active_state()}
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_remove_unknown_returns_false(tmp_path: Path) -> None:
    runner = MonitorRunner(cwd=tmp_path)
    runner.set_dispatch_fn(_Capture())
    removed = await runner.remove("mon-999")
    assert removed is False
    await runner.stop()


@pytest.mark.asyncio
async def test_monitor_runner_stop_kills_all_active(tmp_path: Path) -> None:
    cap = _Capture()
    runner = MonitorRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(
        command="sleep 30", match_regex=None, rate_limit_s=0, response_sink=sink
    )
    runner.spawn(
        command="sleep 30", match_regex=None, rate_limit_s=0, response_sink=sink
    )
    await asyncio.sleep(0.1)
    assert len(runner.active_state()) == 2
    await runner.stop()
    assert runner.active_state() == []
