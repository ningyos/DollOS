"""Tests for MindLoop.iterate — the core MindLoop unit tests."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from dollos.mind.mind_state import MindState, Perception
from dollos.mind.mind_loop import MindLoop
from dollos.mind.perception_queue import PerceptionQueue


class _FakeLLM:
    def __init__(self, returns: str):
        self._returns = returns

    async def stream_completion(self, system, user, prefill, max_tokens=1024, grammar=None):
        class _Chunk:
            def __init__(self, text, done):
                self.text = text
                self.done = done
        yield _Chunk(text=self._returns, done=True)


@pytest.mark.asyncio
async def test_iterate_processes_user_perception_and_says(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)

    fake_llm = _FakeLLM('[{"action": "Say", "text": "hello"}]')
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=fake_llm,
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()
    assert state.iter_count == 1
    assert len(state.recent_outputs) == 1
    assert state.recent_outputs[0].kind == "Say"
    assert state.last_user_at == 1.0
    # State persisted
    assert (tmp_path / "mind_state.json").exists()


@pytest.mark.asyncio
async def test_iterate_handles_malformed_llm_output(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="IdleTick", t=1.0, data={}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)

    fake_llm = _FakeLLM("this is not json at all, just prose")
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=fake_llm,
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()
    # Tolerant fallback: Think action recorded (or Idle if Think fails)
    assert state.iter_count == 1
    # Either a Think output recorded OR no outputs — both acceptable
    # The important thing is we don't crash


@pytest.mark.asyncio
async def test_iterate_idle_tick_no_user(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="IdleTick", t=2.0, data={}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)

    fake_llm = _FakeLLM('[{"action": "Idle"}]')
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=fake_llm,
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()
    assert state.iter_count == 1
    assert state.last_user_at == 0.0  # no UserSpoke → last_user_at unchanged


@pytest.mark.asyncio
async def test_iterate_unknown_action_skipped(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="IdleTick", t=1.0, data={}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)

    fake_llm = _FakeLLM('[{"action": "NonExistentAction", "foo": "bar"}, {"action": "Idle"}]')
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=fake_llm,
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()
    # Should not crash; unknown action is skipped; Idle runs fine
    assert state.iter_count == 1


@pytest.mark.asyncio
async def test_iterate_persists_state(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="IdleTick", t=1.0, data={}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)

    fake_llm = _FakeLLM('[{"action": "Idle"}]')
    persist_path = tmp_path / "mind_state.json"
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=fake_llm,
        system_prompt="SYS",
        state_persist_path=persist_path,
        tool_registry=tool_registry,
    )

    assert not persist_path.exists()
    await loop.iterate()
    assert persist_path.exists()
    # iter_count should be in the persisted file
    import json
    data = json.loads(persist_path.read_text())
    assert data["iter_count"] == 1


@pytest.mark.asyncio
async def test_iterate_multiple_perceptions(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hello"}))
    queue.put(Perception(kind="UserSpoke", t=2.0, data={"text": "world"}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)

    fake_llm = _FakeLLM('[{"action": "Idle"}]')
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=fake_llm,
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()
    # Both perceptions should be in recent_perceptions
    assert len(state.recent_perceptions) == 2
    # last_user_at updated to the later one
    assert state.last_user_at == 2.0


@pytest.mark.asyncio
async def test_shutdown_stops_run(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)

    fake_llm = _FakeLLM('[{"action": "Idle"}]')
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=fake_llm,
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
        idle_interval_s=0.05,  # short timeout for fast test
    )

    # Mark shutdown before run; the while loop should never enter iterate
    loop.shutdown()
    await asyncio.wait_for(loop.run(), timeout=1.0)
    # iter_count stays 0 since we shutdown before any iteration
    assert state.iter_count == 0


@pytest.mark.asyncio
async def test_iterate_think_action_records_thought(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from dollos.tools import MAIN_TOOLS

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="IdleTick", t=1.0, data={}))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)

    fake_llm = _FakeLLM('[{"action": "Think", "text": "I wonder what to do next"}]')
    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=fake_llm,
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()
    assert state.iter_count == 1
    assert len(state.recent_thoughts) >= 1
    # The Think tool appends to recent_thoughts
    assert any("wonder" in t.text for t in state.recent_thoughts)
