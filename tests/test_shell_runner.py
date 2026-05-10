"""ShellRunner unit tests — spawn, completion, timeout, shutdown."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from dollos.events import RawEvent, ShellResultEvent
from dollos.shell_runner import ShellRunner


class _Capture:
    def __init__(self) -> None:
        self.events: list[RawEvent] = []

    def __call__(self, ev: RawEvent) -> None:
        self.events.append(ev)


@pytest.mark.asyncio
async def test_shell_runner_fires_event_on_completion(tmp_path: Path) -> None:
    cap = _Capture()
    runner = ShellRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(command="printf hello", timeout_s=10, response_sink=sink)
    for _ in range(50):
        if cap.events:
            break
        await asyncio.sleep(0.05)
    assert len(cap.events) == 1
    ev = cap.events[0]
    assert isinstance(ev, ShellResultEvent)
    assert ev.command == "printf hello"
    assert ev.status == "ok"
    assert ev.exit_code == 0
    assert "hello" in ev.output
    await runner.stop()


@pytest.mark.asyncio
async def test_shell_runner_timeout(tmp_path: Path) -> None:
    cap = _Capture()
    runner = ShellRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(command="sleep 5", timeout_s=1, response_sink=sink)
    for _ in range(40):
        if cap.events:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.05)
    assert len(cap.events) == 1
    ev = cap.events[0]
    assert ev.status == "timeout"
    assert ev.exit_code is None
    await runner.stop()


@pytest.mark.asyncio
async def test_shell_runner_nonzero_exit(tmp_path: Path) -> None:
    cap = _Capture()
    runner = ShellRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(command="exit 7", timeout_s=10, response_sink=sink)
    for _ in range(50):
        if cap.events:
            break
        await asyncio.sleep(0.05)
    ev = cap.events[0]
    assert ev.status == "nonzero"
    assert ev.exit_code == 7
    await runner.stop()


@pytest.mark.asyncio
async def test_shell_runner_stop_cancels_running(tmp_path: Path) -> None:
    cap = _Capture()
    runner = ShellRunner(cwd=tmp_path)
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(command="sleep 30", timeout_s=60, response_sink=sink)
    await asyncio.sleep(0.1)
    await runner.stop()
    assert cap.events == []
