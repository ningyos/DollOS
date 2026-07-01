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


def test_consolidation_fields_round_trip(tmp_path):
    from dollos.mind.mind_state import MindState, save_state, load_state
    s = MindState()
    s.user_turn_count = 7
    s.last_consolidation_turn = 3
    s.last_consolidation_at = 123.5
    s.last_consolidated_date = "2026-06-29"
    p = tmp_path / "state.json"
    assert save_state(s, p)
    loaded = load_state(p)
    assert loaded.user_turn_count == 7
    assert loaded.last_consolidation_turn == 3
    assert loaded.last_consolidation_at == 123.5
    assert loaded.last_consolidated_date == "2026-06-29"
    assert loaded.recent_tool_failures.maxlen == 10


def test_energy_round_trip_and_clamp(tmp_path):
    from dollos.mind.mind_state import MindState, save_state, load_state
    import json
    s = MindState(); s.energy = 0.4; s.last_energy_restore_at = 12.0
    p = tmp_path / "s.json"; assert save_state(s, p)
    assert load_state(p).energy == 0.4
    assert load_state(p).last_energy_restore_at == 12.0
    # out-of-range clamps on load
    data = json.loads(p.read_text()); data["energy"] = 1.7; p.write_text(json.dumps(data))
    assert load_state(p).energy == 1.0
    data["energy"] = -0.5; p.write_text(json.dumps(data))
    assert load_state(p).energy == 0.0


def test_last_repeat_alert_key_defaults_empty():
    assert MindState().last_repeat_alert_key == ""


def test_last_repeat_alert_key_round_trip(tmp_path):
    from dollos.mind.mind_state import MindState, save_state, load_state
    s = MindState()
    s.last_repeat_alert_key = "SetFocus:focus → same thing:3"
    p = tmp_path / "s.json"
    assert save_state(s, p)
    assert load_state(p).last_repeat_alert_key == "SetFocus:focus → same thing:3"


def test_last_repeat_alert_key_defaults_when_absent_in_old_state(tmp_path):
    """Old persisted state blobs saved before P6 lack this key entirely — must
    load fine with the default, not raise or blank the rest of the state."""
    import json
    from dollos.mind.mind_state import MindState, save_state, load_state
    s = MindState(scratchpad="keep me")
    p = tmp_path / "s.json"
    assert save_state(s, p)
    data = json.loads(p.read_text())
    data.pop("last_repeat_alert_key", None)  # simulate a pre-P6 persisted blob
    p.write_text(json.dumps(data))
    loaded = load_state(p)
    assert loaded.last_repeat_alert_key == ""
    assert loaded.scratchpad == "keep me"
