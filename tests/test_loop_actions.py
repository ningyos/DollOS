"""Tests for Task 6 loop actions: SetFocus, OpenLoop, CloseLoop."""

import pytest

from dollos.mind.mind_state import MindState
from dollos.tools import SetFocus, OpenLoop, CloseLoop
from tests._dispatcher_helpers import _make_mind_ctx


@pytest.mark.asyncio
async def test_set_focus_updates_mind_state(tmp_path):
    state = MindState()
    ctx = _make_mind_ctx(tmp_path, state=state)
    await SetFocus(text="finding line 150").run(ctx)
    assert state.focus == "finding line 150"


@pytest.mark.asyncio
async def test_set_focus_caps_at_200_chars(tmp_path):
    state = MindState()
    ctx = _make_mind_ctx(tmp_path, state=state)
    long = "x" * 500
    await SetFocus(text=long).run(ctx)
    assert len(state.focus) == 200


@pytest.mark.asyncio
async def test_open_close_loop(tmp_path):
    state = MindState()
    ctx = _make_mind_ctx(tmp_path, state=state)
    await OpenLoop(id="t1", desc="check tmp").run(ctx)
    assert len(state.open_loops) == 1
    assert state.open_loops[0].id == "t1"
    assert state.open_loops[0].desc == "check tmp"
    await CloseLoop(id="t1", outcome="done").run(ctx)
    assert len(state.open_loops) == 0


@pytest.mark.asyncio
async def test_close_loop_unknown_id_no_raise(tmp_path):
    state = MindState()
    ctx = _make_mind_ctx(tmp_path, state=state)
    # no loop opened yet
    await CloseLoop(id="missing", outcome="x").run(ctx)
    # no exception; loop list unchanged
    assert state.open_loops == []


@pytest.mark.asyncio
async def test_set_focus_records_output(tmp_path):
    state = MindState()
    ctx = _make_mind_ctx(tmp_path, state=state)
    await SetFocus(text="testing").run(ctx)
    assert len(state.recent_outputs) == 1
    assert state.recent_outputs[0].kind == "SetFocus"
