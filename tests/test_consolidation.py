"""B2 sleep-time consolidation tests."""
from __future__ import annotations
import asyncio
from datetime import date
import pytest

from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from tests._dispatcher_helpers import _make_mind_ctx, _FakeMemSearch
from tests.test_mind_loop import _FakeLLM
from dollos.tools import MAIN_TOOLS


@pytest.mark.asyncio
async def test_user_turn_count_increments_only_on_userspoke(tmp_path):
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    ctx = _make_mind_ctx(tmp_path, sink=asyncio.Queue(), state=state)
    loop = MindLoop(
        state=state, queue=queue, ctx=ctx,
        llm=_FakeLLM("SEEN: x\nTOOL: none\n</think>\n\nhi"),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "s.json",
        tool_registry={c.__name__: c for c in MAIN_TOOLS},
    )
    await loop.iterate()
    assert state.user_turn_count == 1

    # A non-UserSpoke perception must NOT increment it
    queue.put(Perception(kind="ScheduledMoment", t=2.0, data={"text": "alarm"}))
    await loop.iterate()
    assert state.user_turn_count == 1
