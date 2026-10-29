"""ShellRunner unit tests — spawn, completion, timeout, shutdown."""
from __future__ import annotations

import asyncio
import os
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


# ----- run_and_wait: a workflow worker's Shell (plan 2026-10-28 decision 2) -----


def _gone(pid: int) -> bool:
    """True once ``pid`` no longer runs. A killed orphan can linger as a
    zombie until its new parent reaps it; on Linux that counts as gone. The
    reap can land between the two checks, so a vanished /proc entry is gone
    too."""
    try:
        os.kill(pid, 0)
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[-1]
    except (ProcessLookupError, FileNotFoundError):
        return True
    return state.startswith("Z")


async def _wait_gone(pid: int, timeout: float = 2.0) -> bool:
    for _ in range(int(timeout / 0.05)):
        if _gone(pid):
            return True
        await asyncio.sleep(0.05)
    return _gone(pid)


async def _child_pid(pid_file: Path, timeout: float = 3.0) -> int:
    """The pid the command wrote to ``pid_file`` (its backgrounded child)."""
    for _ in range(int(timeout / 0.05)):
        if pid_file.exists() and pid_file.read_text().strip():
            return int(pid_file.read_text())
        await asyncio.sleep(0.05)
    raise AssertionError(f"{pid_file} never written")


@pytest.mark.asyncio
async def test_run_and_wait_returns_result_without_enqueueing(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path / "tool_outputs")
    queue = PerceptionQueue()
    runner = ShellRunner(cwd=tmp_path, perception_queue=queue, tool_output_store=store)
    data = await runner.run_and_wait(
        command="for i in $(seq 1 25); do echo line $i; done", timeout_s=10,
    )
    assert data["tool"] == "Shell"
    assert data["status"] == "ok"
    assert data["exit_code"] == 0
    assert data["line_count"] == 25
    assert data["command"].startswith("for i in")
    assert data["task_id"].startswith("shell-")
    full = store.read(data["output_id"], offset=0, limit=100)
    assert full.lines[0] == "line 1" and full.lines[24] == "line 25"
    # The result is returned, never enqueued for Doll.
    assert await _wait_for_tool_result(queue, timeout=0.3) == []
    await runner.stop()


@pytest.mark.asyncio
async def test_run_and_wait_nonzero_exit(tmp_path: Path) -> None:
    runner, queue = _make_runner(tmp_path)
    data = await runner.run_and_wait(command="echo oops; exit 7", timeout_s=10)
    assert data["status"] == "nonzero"
    assert data["exit_code"] == 7
    assert data["line_count"] == 1
    assert await _wait_for_tool_result(queue, timeout=0.2) == []


@pytest.mark.asyncio
async def test_run_and_wait_timeout_kills_process_group(tmp_path: Path) -> None:
    runner, queue = _make_runner(tmp_path)
    pid_file = tmp_path / "child.pid"
    data = await runner.run_and_wait(
        command=f"sleep 30 & echo $! > {pid_file}; wait", timeout_s=1,
    )
    assert data["status"] == "timeout"
    assert "timed out after 1s" in data["summary"]
    assert await _wait_gone(int(pid_file.read_text()))
    assert await _wait_for_tool_result(queue, timeout=0.2) == []


@pytest.mark.asyncio
async def test_run_and_wait_cancel_kills_process_group(tmp_path: Path) -> None:
    """Cancelling the caller (the workflow's per-agent timeout, its stop())
    kills the command's whole process group."""
    runner, queue = _make_runner(tmp_path)
    pid_file = tmp_path / "child.pid"
    waiting = asyncio.create_task(runner.run_and_wait(
        command=f"sleep 30 & echo $! > {pid_file}; wait", timeout_s=60,
    ))
    pid = await _child_pid(pid_file)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert await _wait_gone(pid)
    assert await _wait_for_tool_result(queue, timeout=0.2) == []
    await runner.stop()


@pytest.mark.asyncio
async def test_timeout_kills_background_child_after_shell_exited(tmp_path: Path) -> None:
    """No ``wait``: bash exits at once and is reaped, while its backgrounded
    child keeps the output pipe open, so the call runs to its timeout. The
    group is gone by then only if the kill does not need the leader alive."""
    runner, _queue = _make_runner(tmp_path)
    pid_file = tmp_path / "child.pid"
    data = await runner.run_and_wait(
        command=f"sleep 30 & echo $! > {pid_file}", timeout_s=1,
    )
    assert data["status"] == "timeout"
    assert await _wait_gone(int(pid_file.read_text()))


@pytest.mark.asyncio
async def test_cancel_kills_background_child_after_shell_exited(tmp_path: Path) -> None:
    runner, _queue = _make_runner(tmp_path)
    pid_file = tmp_path / "child.pid"
    waiting = asyncio.create_task(runner.run_and_wait(
        command=f"sleep 30 & echo $! > {pid_file}", timeout_s=60,
    ))
    pid = await _child_pid(pid_file)
    await asyncio.sleep(0.3)  # bash exits right after the echo and is reaped
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert await _wait_gone(pid)
    await runner.stop()


@pytest.mark.asyncio
async def test_run_and_wait_stop_kills_running_command(tmp_path: Path) -> None:
    """stop() (daemon shutdown) kills a command a worker is waiting on, as it
    does a spawned one."""
    runner, _queue = _make_runner(tmp_path)
    pid_file = tmp_path / "child.pid"
    waiting = asyncio.create_task(runner.run_and_wait(
        command=f"sleep 30 & echo $! > {pid_file}; wait", timeout_s=60,
    ))
    pid = await _child_pid(pid_file)
    await asyncio.wait_for(runner.stop(), timeout=2.0)
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert await _wait_gone(pid)


@pytest.mark.asyncio
async def test_run_and_wait_refused_while_stopping(tmp_path: Path) -> None:
    runner, _queue = _make_runner(tmp_path)
    await runner.stop()
    marker = tmp_path / "ran"
    data = await runner.run_and_wait(command=f"touch {marker}", timeout_s=10)
    assert data["status"] == "error"
    assert "stopping" in data["summary"]
    assert data["output_id"] is None
    await asyncio.sleep(0.2)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_run_and_wait_runner_error(tmp_path: Path) -> None:
    """A subprocess that cannot start (cwd gone) is a runner error."""
    runner = ShellRunner(
        cwd=tmp_path / "missing",
        perception_queue=PerceptionQueue(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
    )
    data = await runner.run_and_wait(command="echo hi", timeout_s=10)
    assert data["status"] == "error"
    assert data["summary"].startswith("runner error:")
    assert data["output_id"] is None


# ----- Shell preview: head + tail with a marker (rerun 2026-10-29, 3.1 rep 3) -----


def _stored(tmp_path: Path, n: int) -> tuple[ToolOutputStore, str]:
    store = ToolOutputStore(tmp_path / "tool_outputs")
    out_id = store.write("".join(f"line {i}\n" for i in range(1, n + 1)))
    return store, out_id


def test_shell_preview_long_output_shows_head_marker_and_tail(tmp_path: Path) -> None:
    from dollos.shell_runner import (
        SHELL_PREVIEW_HEAD_LINES,
        SHELL_PREVIEW_TAIL_LINES,
        shell_preview,
    )

    store, out_id = _stored(tmp_path, 42)
    lines = shell_preview(store, out_id)
    assert SHELL_PREVIEW_HEAD_LINES + SHELL_PREVIEW_TAIL_LINES == 10
    assert lines[0] == "line 1"
    assert lines[-1] == "line 42"
    assert lines[:6] == [f"line {i}" for i in range(1, 7)]
    assert lines[-4:] == [f"line {i}" for i in range(39, 43)]
    assert lines[6] == (
        f"… 32 lines not shown (ReadToolOutput id={out_id} offset=6 limit=32) …"
    )
    assert len(lines) == 11


def test_shell_preview_ten_lines_shown_whole(tmp_path: Path) -> None:
    from dollos.shell_runner import shell_preview

    store, out_id = _stored(tmp_path, 10)
    assert shell_preview(store, out_id) == [f"line {i}" for i in range(1, 11)]


def test_shell_preview_eleven_lines_hides_one(tmp_path: Path) -> None:
    from dollos.shell_runner import shell_preview

    store, out_id = _stored(tmp_path, 11)
    lines = shell_preview(store, out_id)
    assert lines[:6] == [f"line {i}" for i in range(1, 7)]
    assert lines[6] == (
        f"… 1 line not shown (ReadToolOutput id={out_id} offset=6 limit=1) …"
    )
    assert lines[7:] == [f"line {i}" for i in range(8, 12)]


@pytest.mark.asyncio
async def test_long_shell_perception_shows_the_tail(tmp_path: Path) -> None:
    """Doll's long Shell: the ToolResultArrived perception carries the same
    head + tail preview, and its rendered line shows the last line."""
    from dollos.mind.mind_prompt import _percep_body

    runner, queue = _make_runner(tmp_path)
    runner.spawn(command="seq 1 42", timeout_s=10)
    (p,) = await _wait_for_tool_result(queue)
    out_id = p.data["output_id"]
    body = _percep_body(p).splitlines()
    assert f"output_id {out_id}, 42 lines" in body[0]
    assert body[1] == "    1"
    assert body[-1] == "    42"
    assert (
        f"    … 32 lines not shown (ReadToolOutput id={out_id} offset=6 limit=32) …"
        in body
    )
    assert "    37" not in body  # hidden middle line
    await runner.stop()


@pytest.mark.asyncio
async def test_short_output_perception_shown_whole(tmp_path: Path) -> None:
    from dollos.mind.mind_prompt import _percep_body

    runner, queue = _make_runner(tmp_path)
    runner.spawn(command="seq 1 10", timeout_s=10)
    (p,) = await _wait_for_tool_result(queue)
    body = _percep_body(p).splitlines()
    assert body[1:] == [f"    {i}" for i in range(1, 11)]
    assert not any("not shown" in line for line in body)
    await runner.stop()


def test_shell_preview_caps_each_line(tmp_path: Path) -> None:
    """A preview line longer than the cap is cut with a visible marker, so
    one minified-JSON line cannot fill the perception, WAL or delta."""
    from dollos.shell_runner import SHELL_PREVIEW_LINE_MAX_CHARS, shell_preview

    store = ToolOutputStore(tmp_path / "tool_outputs")
    long = "x" * 5000
    out_id = store.write(f"{long}\nshort\n")
    lines = shell_preview(store, out_id)
    assert lines[0] == f"{'x' * SHELL_PREVIEW_LINE_MAX_CHARS}… (line cut, 5000 chars)"
    assert lines[1] == "short"


def test_shell_preview_marker_singular(tmp_path: Path) -> None:
    from dollos.shell_runner import shell_preview

    store, out_id = _stored(tmp_path, 11)
    assert shell_preview(store, out_id)[6] == (
        f"… 1 line not shown (ReadToolOutput id={out_id} offset=6 limit=1) …"
    )


@pytest.mark.asyncio
async def test_long_shell_perception_preview_lines_are_capped(tmp_path: Path) -> None:
    from dollos.shell_runner import SHELL_PREVIEW_LINE_MAX_CHARS

    runner, queue = _make_runner(tmp_path)
    runner.spawn(command="for i in $(seq 1 20); do printf '%05000d\\n' $i; done", timeout_s=10)
    (p,) = await _wait_for_tool_result(queue)
    preview = p.data["preview"].splitlines()
    assert len(preview) == 11
    assert all(len(line) <= SHELL_PREVIEW_LINE_MAX_CHARS + 40 for line in preview)
    await runner.stop()
