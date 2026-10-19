"""Task 5 — mind_loop wiring for PinSelf (registry injection + offered list + ctor flag).

Verifies the LIVE reflection-turn injection path in mind_loop.py:
`_active_tool_registry()` (what may run) / `_offered_tools()` (what the model
is shown) — NOT the dead `REFLECTION_TOOLS` constant in tools.py, which has
no runtime consumer.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import MindState
from dollos.mind.perception_queue import PerceptionQueue
from dollos.tools import MAIN_TOOLS, PinSelf
from tests._dispatcher_helpers import _make_mind_ctx
from tests.test_mind_loop import _FakeLLM


def _build_mind_loop(tmp_path: Path, self_profile_enabled: bool) -> MindLoop:
    """Minimal MindLoop construction, mirroring tests/test_mind_loop.py's
    `_make_mind_loop` / tests/test_energy.py's `_make_loop` helpers. No shared
    `make_mind_loop` fixture exists in this repo yet, so this replicates the
    minimal construction locally per controller guidance.
    """
    state = MindState()
    queue = PerceptionQueue(wal=None)
    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(tmp_path, state=state)
    llm = _FakeLLM(
        "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: none\n</think>\n\nhi"
    )
    return MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=llm,
        system_prompt="SYS",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
        self_profile_enabled=self_profile_enabled,
    )


@pytest.fixture
def make_mind_loop(tmp_path):
    """Factory fixture: make_mind_loop(self_profile_enabled=...) -> MindLoop."""

    def _factory(self_profile_enabled: bool = False) -> MindLoop:
        return _build_mind_loop(tmp_path, self_profile_enabled=self_profile_enabled)

    return _factory


def test_pinself_offered_on_internal_turns_when_enabled(make_mind_loop):
    # PinSelf's schema is always in the internal list when self_profile is on
    # (fixed per tier), and its result — including soft failures like "target
    # not found", returned as success strings — goes back like every tool
    # result, so a failed pin can be retried the same turn.
    assert "PinSelf" in make_mind_loop(self_profile_enabled=True)._offered_tools()
    assert "PinSelf" not in make_mind_loop(self_profile_enabled=False)._offered_tools()


def test_reflection_registry_includes_pinself_when_enabled(make_mind_loop):
    loop = make_mind_loop(self_profile_enabled=True)
    loop._is_reflection = True
    reg = loop._active_tool_registry()
    assert "PinSelf" in reg
    assert reg["PinSelf"] is PinSelf


def test_reflection_registry_excludes_pinself_when_disabled(make_mind_loop):
    loop = make_mind_loop(self_profile_enabled=False)
    loop._is_reflection = True
    reg = loop._active_tool_registry()
    assert "PinSelf" not in reg


def test_non_reflection_never_has_pinself(make_mind_loop):
    loop = make_mind_loop(self_profile_enabled=True)
    loop._is_reflection = False
    assert "PinSelf" not in loop._active_tool_registry()
