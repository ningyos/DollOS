"""Test MindState persistence — atomic save/load with state refresh."""
import json
import time
from pathlib import Path

import pytest

from dollos.mind.mind_state import (
    MindState,
    OpenLoop,
    Perception,
    save_state,
    load_state,
)


def test_round_trip(tmp_path: Path) -> None:
    """Test that save and load preserve all fields correctly."""
    s = MindState(focus="working on blender", iter_count=42)
    s.open_loops.append(OpenLoop(id="x", desc="d", opened_at=100.0))
    s.recent_perceptions.append(
        Perception(kind="UserSpoke", t=99.0, data={"text": "hi"})
    )
    path = tmp_path / "mind_state.json"
    save_state(s, path)
    loaded = load_state(path)
    assert loaded.focus == "working on blender"
    assert loaded.iter_count == 42
    assert len(loaded.open_loops) == 1 and loaded.open_loops[0].id == "x"
    assert len(loaded.recent_perceptions) == 1
    assert loaded.recent_perceptions[0].data["text"] == "hi"


def test_load_missing_file_returns_fresh(tmp_path: Path) -> None:
    """Test that loading a missing file returns a fresh MindState."""
    loaded = load_state(tmp_path / "absent.json")
    assert loaded.iter_count == 0


def test_load_malformed_returns_fresh(tmp_path: Path, caplog) -> None:
    """Test that malformed JSON logs error and returns fresh MindState."""
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    loaded = load_state(path)
    assert loaded.iter_count == 0


def test_atomic_write_no_partial_on_crash(tmp_path: Path) -> None:
    """Test that atomic write via tmp file leaves no partial files."""
    s = MindState(focus="task A", iter_count=1)
    path = tmp_path / "mind_state.json"
    save_state(s, path)
    assert not (tmp_path / "mind_state.json.tmp").exists()
    assert path.exists()
    s.focus = "task B"
    s.iter_count = 2
    save_state(s, path)
    loaded = load_state(path)
    assert loaded.focus == "task B"


def test_energy_refreshes_on_load(tmp_path: Path) -> None:
    """Test that energy is reset to 1.0 when loading."""
    s = MindState(energy=0.2)
    path = tmp_path / "mind_state.json"
    save_state(s, path)
    loaded = load_state(path)
    assert loaded.energy == 1.0


def test_session_started_at_refreshes_on_load(tmp_path: Path) -> None:
    """Test that session_started_at is reset to current time when loading."""
    s = MindState()
    old_session = s.session_started_at
    path = tmp_path / "mind_state.json"
    save_state(s, path)
    time.sleep(0.05)
    loaded = load_state(path)
    assert loaded.session_started_at > old_session
