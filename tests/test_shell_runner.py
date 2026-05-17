"""ShellRunner unit tests — spawn, completion, timeout, shutdown."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dollos.mind.mind_state import Perception
from dollos.mind.perception_queue import PerceptionQueue
from dollos.shell_runner import ShellRunner
from dollos.tool_outputs import ToolOutputStore


def _make_runner(tmp_path: Path) -> tuple[ShellRunner, PerceptionQueue]:
    queue = PerceptionQueue()
    runner = ShellRunner(
        cwd=tmp_path,
        perception_queue=queue,
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
    )
    return runner, queue


async def _wait_for_tool_result(queue: PerceptionQueue, timeout: float = 3.0) -> list[Perception]:
    """Poll until at least one ToolResultArrived perception arrives."""
    collected: list[Perception] = []
    for _ in range(int(timeout / 0.05)):
        perceptions = await queue.drain(timeout_s=0.05)
        collected.extend(p for p in perceptions if p.kind == "ToolResultArrived")
        if collected:
            return collected
    return collected


@pytest.mark.asyncio
async def test_shell_runner_fires_event_on_completion(tmp_path: Path) -> None:
    runner, queue = _make_runner(tmp_path)
    runner.spawn(command="printf hello", timeout_s=10)
    perceptions = await _wait_for_tool_result(queue)
    assert len(perceptions) == 1
    p = perceptions[0]
    assert p.kind == "ToolResultArrived"
    assert p.data["tool"] == "Shell"
    assert p.data["status"] == "ok"
    assert p.data["exit_code"] == 0
    assert "hello" in p.data["summary"]
    await runner.stop()


@pytest.mark.asyncio
async def test_shell_runner_timeout(tmp_path: Path) -> None:
    runner, queue = _make_runner(tmp_path)
    runner.spawn(command="sleep 5", timeout_s=1)
    perceptions = await _wait_for_tool_result(queue, timeout=3.0)
    assert len(perceptions) == 1
    p = perceptions[0]
    assert p.data["status"] == "timeout"
    await runner.stop()


@pytest.mark.asyncio
async def test_shell_runner_nonzero_exit(tmp_path: Path) -> None:
    runner, queue = _make_runner(tmp_path)
    runner.spawn(command="exit 7", timeout_s=10)
    perceptions = await _wait_for_tool_result(queue)
    p = perceptions[0]
    assert p.data["status"] == "nonzero"
    assert p.data["exit_code"] == 7
    await runner.stop()


@pytest.mark.asyncio
async def test_shell_writes_full_output_to_store_and_returns_id(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    queue = PerceptionQueue()
    runner = ShellRunner(cwd=tmp_path, perception_queue=queue, tool_output_store=store)
    # Generate 100 lines of output
    cmd = "for i in $(seq 1 100); do echo line $i; done"
    runner.spawn(command=cmd, timeout_s=10)
    perceptions = await _wait_for_tool_result(queue)
    assert len(perceptions) == 1
    data = perceptions[0].data
    assert data["status"] == "ok"
    assert data["line_count"] == 100
    assert data["output_id"] is not None
    # Store has the full body
    full = store.read(data["output_id"], offset=0, limit=200)
    assert full.total_lines == 100
    assert full.lines[0] == "line 1"
    assert full.lines[99] == "line 100"


@pytest.mark.asyncio
async def test_shell_runner_stop_cancels_running(tmp_path: Path) -> None:
    runner, queue = _make_runner(tmp_path)
    runner.spawn(command="sleep 30", timeout_s=60)
    await asyncio.sleep(0.1)
    await runner.stop()
    # No ToolResultArrived should arrive (cancelled mid-run)
    result_perceptions = await _wait_for_tool_result(queue, timeout=0.3)
    assert result_perceptions == []
