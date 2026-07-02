"""self_history read helpers for 慢變演化 Plan 2 (spec §3.1/§3.5/§5)."""
from dollos.mind import self_history


def _seed(p):
    self_history.log_event(p, kind="pin_add", turn=1, external_ctx=False,
                           section="self", id="s1", text="A")
    self_history.log_event(p, kind="evo_adopt", text="第一版的我", old_text=None,
                           drift_score=None)
    self_history.log_event(p, kind="pin_reconfirm", turn=9, external_ctx=False,
                           section="self", id="s1", text="A")
    self_history.log_event(p, kind="evo_adopt", text="第二版的我",
                           old_text="第一版的我", drift_score=0.42)


def test_read_events_returns_all_in_order(tmp_path):
    p = tmp_path / "self_history.jsonl"
    _seed(p)
    kinds = [e["kind"] for e in self_history.read_events(p)]
    assert kinds == ["pin_add", "evo_adopt", "pin_reconfirm", "evo_adopt"]


def test_read_events_missing_file_is_empty(tmp_path):
    assert self_history.read_events(tmp_path / "nope.jsonl") == []


def test_latest_adopt_is_most_recent(tmp_path):
    p = tmp_path / "self_history.jsonl"
    _seed(p)
    ev = self_history.latest_adopt(p)
    assert ev["text"] == "第二版的我" and ev["old_text"] == "第一版的我"


def test_sanctioned_text_is_latest_adopt_text(tmp_path):
    p = tmp_path / "self_history.jsonl"
    _seed(p)
    assert self_history.sanctioned_text(p) == "第二版的我"


def test_sanctioned_text_none_before_any_adoption(tmp_path):
    p = tmp_path / "self_history.jsonl"
    self_history.log_event(p, kind="pin_add", turn=1, external_ctx=False,
                           section="self", id="s1", text="A")
    assert self_history.sanctioned_text(p) is None
    assert self_history.latest_adopt(p) is None


def test_generation_counts_adopt_events(tmp_path):
    p = tmp_path / "self_history.jsonl"
    assert self_history.generation(p) == 0
    _seed(p)
    assert self_history.generation(p) == 2


def test_latest_external_edit_text(tmp_path):
    p = tmp_path / "self_history.jsonl"
    self_history.log_event(p, kind="external_edit", text="有人手動改的", reason=None)
    self_history.log_event(p, kind="external_edit", reason="mechanical:太短")  # no text
    assert self_history.latest_external_edit_text(p) == "有人手動改的"


def test_latest_external_edit_text_none(tmp_path):
    p = tmp_path / "self_history.jsonl"
    _seed(p)
    assert self_history.latest_external_edit_text(p) is None
