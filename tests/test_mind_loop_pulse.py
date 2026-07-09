"""PulseMoment turn — tools narrow to PULSE_TOOLS, speech stays ON
(spec 2026-07-09 §5.2/§5.3, Task 5).

Mirrors tests/test_mind_loop_agenda_registry.py's pattern exactly: drive a
real ``_run_one_turn`` with a pure PulseMoment batch (not a hand-poked
attribute), then assert the resulting ``_is_pulse`` flag + the narrowed
``_active_tool_registry()`` + — the entire point of this feature, unlike
AgendaMoment/DiaryMoment — that speech is NOT suppressed.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from dollos.mind.mind_state import Perception
from dollos.tools import PULSE_TOOLS
from tests._mindloop_factory import make_mindloop


def _pulse_moment() -> Perception:
    return Perception(kind="PulseMoment", t=time.time(), data={})


def _user_perception(text: str = "hi") -> Perception:
    return Perception(kind="UserSpoke", t=time.time(), data={"text": text})


@pytest.mark.asyncio
async def test_pure_pulse_turn_narrows_to_pulse_tools(tmp_path):
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn([_pulse_moment()])

    assert ml._is_pulse is True
    reg = ml._active_tool_registry()
    assert set(reg.keys()) == set(PULSE_TOOLS)
    assert "Shell" not in reg
    assert "SpawnWorkflow" not in reg


@pytest.mark.asyncio
async def test_pulse_cobatched_with_userspoke_keeps_full_registry(tmp_path):
    """A live UserSpoke co-batched with a PulseMoment must NEVER be silently
    narrowed to PULSE_TOOLS — same whitewash guard as agenda/diary."""
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn([_pulse_moment(), _user_perception("hi")])

    assert ml._is_pulse is False, (
        "_is_pulse must be False when a live UserSpoke is co-batched — "
        "the user's request must never be silently restricted to PULSE_TOOLS"
    )
    reg = ml._active_tool_registry()
    assert "Shell" in reg
    assert set(PULSE_TOOLS).issubset(reg.keys())  # full registry is a superset


def _drain_sink(sink: asyncio.Queue) -> list:
    items = []
    while not sink.empty():
        items.append(sink.get_nowait())
    return items


@pytest.mark.asyncio
async def test_pure_pulse_turn_still_emits_speech_to_sink(tmp_path):
    """Contrast with test_mind_loop_agenda_registry.py's
    test_agenda_moment_turn_does_not_emit_speech_to_sink: a pure PulseMoment
    turn's speech is NOT suppressed. Tool-narrowing and speech-suppression
    are separate axes — pulse only narrows the former."""
    sink: asyncio.Queue = asyncio.Queue()
    ml = make_mindloop(memory_root=tmp_path, sink=sink)

    await ml._run_one_turn([_pulse_moment()])

    items = _drain_sink(sink)
    speech_items = [i for i in items if i is not None]
    assert speech_items != [], "PulseMoment turn speech must NOT be suppressed"
