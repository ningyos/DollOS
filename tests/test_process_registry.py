"""Tests for ProcessRegistry — Phase 2 async Shell handle tracker."""

from __future__ import annotations

import asyncio

import pytest

from dollos.process_registry import ManagedProcess, ProcessRegistry


async def _spawn(command: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


@pytest.mark.asyncio
async def test_register_returns_unique_handles():
    registry = ProcessRegistry()
    p1 = await _spawn("true")
    p2 = await _spawn("true")
    h1 = registry.register(p1, "true")
    h2 = registry.register(p2, "true")
    assert h1 == "sh-1"
    assert h2 == "sh-2"
    await p1.wait()
    await p2.wait()


@pytest.mark.asyncio
async def test_get_returns_managed_process():
    registry = ProcessRegistry()
    proc = await _spawn("true")
    handle = registry.register(proc, "true")
    mp = registry.get(handle)
    assert isinstance(mp, ManagedProcess)
    assert mp.handle == handle
    assert mp.proc is proc
    assert mp.command == "true"
    await proc.wait()


def test_get_unknown_returns_none():
    registry = ProcessRegistry()
    assert registry.get("sh-999") is None


@pytest.mark.asyncio
async def test_remove_drops_handle():
    registry = ProcessRegistry()
    proc = await _spawn("true")
    handle = registry.register(proc, "true")
    assert registry.get(handle) is not None
    registry.remove(handle)
    assert registry.get(handle) is None
    await proc.wait()


def test_remove_unknown_is_silent():
    registry = ProcessRegistry()
    registry.remove("sh-999")  # no exception


@pytest.mark.asyncio
async def test_shutdown_kills_running_processes():
    registry = ProcessRegistry()
    proc = await _spawn("sleep 30")
    registry.register(proc, "sleep 30")
    assert proc.returncode is None
    await registry.shutdown()
    assert proc.returncode is not None
    # Registry cleared.
    assert registry.get("sh-1") is None


@pytest.mark.asyncio
async def test_shutdown_skips_already_finished_processes():
    registry = ProcessRegistry()
    proc = await _spawn("true")
    await proc.wait()
    rc_before = proc.returncode
    registry.register(proc, "true")
    await registry.shutdown()
    # returncode unchanged (no second kill).
    assert proc.returncode == rc_before
    assert registry.get("sh-1") is None
