import time
from collections import deque

import pytest

from dollos.mind.mind_state import (
    MindState, ActiveTask, PendingEvent, OpenLoop,
    Perception, OutputRecord,
)


def test_mindstate_initial_defaults() -> None:
    s = MindState()
    assert s.focus == "idle"
    assert s.scratchpad == ""
    assert s.iter_count == 0
    assert s.active_tasks == []
    assert s.pending_events == []
    assert s.open_loops == []
    assert len(s.recent_perceptions) == 0
    assert len(s.recent_outputs) == 0


def test_deque_maxlens_default() -> None:
    s = MindState()
    assert s.recent_perceptions.maxlen == 20
    assert s.recent_outputs.maxlen == 15


def test_deque_maxlens_configurable() -> None:
    s = MindState(
        recent_perceptions=deque(maxlen=5),
        recent_outputs=deque(maxlen=5),
    )
    for i in range(10):
        s.recent_perceptions.append(Perception(kind="UserSpoke", t=float(i), data={}))
    assert len(s.recent_perceptions) == 5
    assert s.recent_perceptions[0].t == 5.0


def test_open_loop_add_remove() -> None:
    s = MindState()
    s.open_loops.append(OpenLoop(id="loop1", desc="check tmp", opened_at=time.time()))
    assert len(s.open_loops) == 1
    s.open_loops = [ol for ol in s.open_loops if ol.id != "loop1"]
    assert s.open_loops == []


def test_active_task_elapsed_s() -> None:
    started = time.time() - 5.0
    t = ActiveTask(task_id="shell-1", kind="shell", summary="ls /tmp", started_at=started)
    elapsed = t.elapsed_s
    assert 4.5 <= elapsed <= 5.5


def test_tool_memory_fields_roundtrip(tmp_path):
    from dollos.mind.mind_state import ToolFailure, save_state, load_state
    s = MindState()
    s.tool_stats = {"Shell": {"ok": 3, "fail": 1}}
    s.recent_tool_failures = deque(
        [ToolFailure(t=123.0, tool="Shell", detail="timeout")], maxlen=10
    )
    p = tmp_path / "s.json"
    assert save_state(s, p)
    loaded = load_state(p)
    assert loaded.tool_stats == {"Shell": {"ok": 3, "fail": 1}}
    assert len(loaded.recent_tool_failures) == 1
    assert loaded.recent_tool_failures[0].tool == "Shell"
    assert loaded.recent_tool_failures[0].detail == "timeout"
    assert loaded.recent_tool_failures.maxlen == 10
