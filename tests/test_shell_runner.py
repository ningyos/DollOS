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


# ----- Regression: I4 — task_id preserved in all Perception paths -----


@pytest.mark.asyncio
async def test_shell_runner_task_id_preserved_in_success_perception(tmp_path: Path) -> None:
    """Success path must emit the uuid-form task_id, not f"shell-{command[:20]}"."""
    runner, queue = _make_runner(tmp_path)
    command = "printf hello"
    runner.spawn(command=command, timeout_s=10)
    perceptions = await _wait_for_tool_result(queue)
    assert len(perceptions) == 1
    task_id = perceptions[0].data["task_id"]
    # Must be the uuid form (shell-<8 hex chars>), not the command-prefix form.
    assert task_id.startswith("shell-"), f"unexpected task_id prefix: {task_id!r}"
    # The uuid form has exactly 8 hex chars after "shell-"; the old bug produced
    # "shell-printf hello" (17 chars after "shell-").
    suffix = task_id[len("shell-"):]
    assert len(suffix) == 8, f"expected 8-char hex suffix, got {suffix!r}"
    assert all(c in "0123456789abcdef" for c in suffix), (
        f"suffix is not hex: {suffix!r}"
    )
    # Double-check: not the legacy command-prefix form.
    assert task_id != f"shell-{command[:20]}", (
        "task_id must not be the old f\"shell-{command[:20]}\" form"
    )
    await runner.stop()


@pytest.mark.asyncio
async def test_shell_runner_task_id_preserved_in_nonzero_perception(tmp_path: Path) -> None:
    """Nonzero exit (also success path) must emit the uuid-form task_id."""
    runner, queue = _make_runner(tmp_path)
    command = "exit 1"
    runner.spawn(command=command, timeout_s=10)
    perceptions = await _wait_for_tool_result(queue)
    assert len(perceptions) == 1
    task_id = perceptions[0].data["task_id"]
    suffix = task_id[len("shell-"):]
    assert len(suffix) == 8 and all(c in "0123456789abcdef" for c in suffix), (
        f"task_id not in uuid form: {task_id!r}"
    )
    await runner.stop()
