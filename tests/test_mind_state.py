from collections import deque

import pytest

from dollos.mind.mind_state import (
    MindState, PendingEvent,
    OutputRecord,
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


def test_default_mood_is_an_english_word_moodtool_accepts() -> None:
    """Plan 2026-10-19 gura-english-speech Task 4: the [Mind state] mood
    label is English; the default is one word MoodTool itself would take."""
    from dollos.tools import MoodTool

    s = MindState()
    assert s.mood.emotion == "calm"
    assert MoodTool(emotion=s.mood.emotion).emotion == "calm"


def test_deque_maxlens_default() -> None:
    s = MindState()
    assert s.recent_perceptions.maxlen == 20
    assert s.recent_outputs.maxlen == 15


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


def test_old_state_file_with_scratchpad_still_loads(tmp_path):
    """A state file saved while Doll still had Scratchpad keeps its text."""
    import json

    from dollos.mind.mind_state import load_state, save_state
    p = tmp_path / "s.json"
    assert save_state(MindState(focus="old", scratchpad="waiting for ls"), p)
    data = json.loads(p.read_text())
    assert data["scratchpad"] == "waiting for ls"
    loaded = load_state(p)
    assert loaded.scratchpad == "waiting for ls"
    assert loaded.focus == "old"


def test_load_state_drops_legacy_monitor_perceptions(tmp_path):
    """A state file saved before the monitor feature was removed (plan
    2026-10-29 decision 3) may hold MonitorFired / MonitorEnded perceptions
    and a "monitor" active task. It loads; the monitor perceptions are dropped."""
    import json

    from dollos.mind.mind_state import load_state

    path = tmp_path / "mind_state.json"
    path.write_text(json.dumps({
        "recent_perceptions": [
            {"kind": "MonitorFired", "t": 1.0,
             "data": {"monitor_id": "mon-1", "line": "x"}, "seq": None},
            {"kind": "UserSpoke", "t": 2.0, "data": {"text": "hi"}, "seq": None},
            {"kind": "MonitorEnded", "t": 3.0,
             "data": {"monitor_id": "mon-1", "exit_status": 0}, "seq": None},
        ],
        "active_tasks": [
            {"task_id": "mon-1", "kind": "monitor", "summary": "tail", "started_at": 1.0},
        ],
    }))
    s = load_state(path)
    assert [p.kind for p in s.recent_perceptions] == ["UserSpoke"]
    assert path.exists()  # not quarantined
