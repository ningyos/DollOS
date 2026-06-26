import json
import time
from pathlib import Path

import pytest

from dollos.mind.mind_state import (
    MindState, Mood, Perception, load_state, save_state,
)
# New symbol introduced by this task:
from dollos.mind.mind_state import MindStateLoadError


def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def test_additive_top_level_field_preserved_not_blanked(tmp_path):
    """A future additive top-level key must NOT blank the whole self."""
    p = tmp_path / "mind_state.json"
    s = MindState(mood=Mood(emotion="開心", reason="x"), scratchpad="keep me")
    save_state(s, p)
    data = json.loads(p.read_text())
    data["some_future_field"] = 123          # additive drift
    _write(p, data)

    loaded = load_state(p)
    assert loaded.scratchpad == "keep me"     # not blanked
    assert loaded.mood.emotion == "開心"


def test_additive_nested_field_preserved(tmp_path):
    """An extra key inside a nested record must be tolerated, record kept."""
    p = tmp_path / "mind_state.json"
    s = MindState()
    s.recent_perceptions.append(
        Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"})
    )
    save_state(s, p)
    data = json.loads(p.read_text())
    data["recent_perceptions"][0]["future_inner_field"] = "x"   # nested drift
    _write(p, data)

    loaded = load_state(p)
    assert len(loaded.recent_perceptions) == 1
    assert loaded.recent_perceptions[0].data == {"text": "hi"}


def test_missing_file_is_cold_start(tmp_path):
    loaded = load_state(tmp_path / "nope.json")
    assert isinstance(loaded, MindState)
    assert loaded.scratchpad == ""


def test_corrupt_json_raises_and_quarantines(tmp_path):
    """Genuine corruption surfaces (raises) + quarantines — never blanks."""
    p = tmp_path / "mind_state.json"
    p.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(MindStateLoadError):
        load_state(p)
    quarantined = list(tmp_path.glob("mind_state.json.corrupt-*"))
    assert quarantined, "corrupt file must be quarantined, not left in place"


def test_save_state_returns_true_on_success(tmp_path):
    from dollos.mind.mind_state import MindState, save_state
    assert save_state(MindState(), tmp_path / "s.json") is True


def test_save_state_returns_false_on_failure(tmp_path):
    from dollos.mind.mind_state import MindState, save_state
    # Parent is a file, so mkdir/open fails -> save returns False, does not raise.
    bad = tmp_path / "afile"
    bad.write_text("x")
    assert save_state(MindState(), bad / "s.json") is False
