"""ShellRunner unit tests — spawn, completion, timeout, shutdown."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from dollos.events import RawEvent, ShellResultEvent
from dollos.shell_runner import ShellRunner
from dollos.tool_outputs import ToolOutputStore


class _Capture:
    def __init__(self) -> None:
        self.events: list[RawEvent] = []

    def __call__(self, ev: RawEvent) -> None:
        self.events.append(ev)


@pytest.mark.asyncio
async def test_shell_runner_fires_event_on_completion(tmp_path: Path) -> None:
    cap = _Capture()
    runner = ShellRunner(cwd=tmp_path, tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"))
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
    runner = ShellRunner(cwd=tmp_path, tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"))
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
    runner = ShellRunner(cwd=tmp_path, tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"))
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
async def test_shell_writes_full_output_to_store_and_returns_id(
    tmp_path: Path,
) -> None:
    store = ToolOutputStore(tmp_path)
    events: list[ShellResultEvent] = []

    def dispatch(e: RawEvent) -> None:
        events.append(e)  # type: ignore[arg-type]

    runner = ShellRunner(cwd=tmp_path, dispatch_fn=dispatch, tool_output_store=store)
    sink: asyncio.Queue = asyncio.Queue()
    # Generate 100 lines of output
    cmd = "for i in $(seq 1 100); do echo line $i; done"
    runner.spawn(command=cmd, timeout_s=10, response_sink=sink)
    # Wait for event
    for _ in range(50):
        if events:
            break
        await asyncio.sleep(0.1)

    assert len(events) == 1
    evt = events[0]
    assert evt.status == "ok"
    assert evt.line_count == 100
    assert evt.output_id is not None
    # `output` is preview head, not the full thing
    assert len(evt.output.splitlines()) <= 15
    # Store has the full body
    full = store.read(evt.output_id, offset=0, limit=200)
    assert full.total_lines == 100
    assert full.lines[0] == "line 1"
    assert full.lines[99] == "line 100"


@pytest.mark.asyncio
async def test_shell_runner_stop_cancels_running(tmp_path: Path) -> None:
    cap = _Capture()
    runner = ShellRunner(cwd=tmp_path, tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"))
    runner.set_dispatch_fn(cap)
    sink: asyncio.Queue = asyncio.Queue()
    runner.spawn(command="sleep 30", timeout_s=60, response_sink=sink)
    await asyncio.sleep(0.1)
    await runner.stop()
    assert cap.events == []
