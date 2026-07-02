"""Task 5 — mind_loop wiring for PinSelf (registry injection + refeed + ctor flag).

Verifies the LIVE reflection-turn injection path in mind_loop.py:
`_active_tool_registry()` / `_active_grammar()` — NOT the dead `REFLECTION_TOOLS`
constant in tools.py, which has no runtime consumer.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from dollos.mind.mind_loop import IN_TURN_REFEED_TOOLS, MindLoop
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


def test_pinself_in_refeed():
    # PinSelf is in the allowlist so a soft-failure (replace/remove target not
    # found, over-cap) — returned as a success=True string — can retry same-turn.
    assert "PinSelf" in IN_TURN_REFEED_TOOLS
    assert "Recall" in IN_TURN_REFEED_TOOLS  # guard the intended final state


def test_reflection_registry_includes_pinself_when_enabled(make_mind_loop):
    loop = make_mind_loop(self_profile_enabled=True)
    loop._is_reflection = True
    reg = loop._active_tool_registry()
    assert "PinSelf" in reg
    assert reg["PinSelf"] is PinSelf
    assert "PinSelf" in (loop._active_grammar() or "")


def test_reflection_registry_excludes_pinself_when_disabled(make_mind_loop):
    loop = make_mind_loop(self_profile_enabled=False)
    loop._is_reflection = True
    reg = loop._active_tool_registry()
    assert "PinSelf" not in reg
    assert "PinSelf" not in (loop._active_grammar() or "")


def test_non_reflection_never_has_pinself(make_mind_loop):
    loop = make_mind_loop(self_profile_enabled=True)
    loop._is_reflection = False
    assert "PinSelf" not in loop._active_tool_registry()
