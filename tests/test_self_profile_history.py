"""self_profile.apply → self_history pin-event logging (spec 2026-07-02 §3.2)."""
import json

import pytest

from dollos.mind import self_profile, self_history


def _events(hist):
    if not hist.exists():
        return []
    return [json.loads(l) for l in hist.read_text(encoding="utf-8").splitlines()]


def _apply(tmp_path, hist, *, op, section="self", target="", text="", turn=1,
           external_ctx=False, max_chars=1200):
    return self_profile.apply(
        tmp_path / "self_profile.md", section=section, op=op, target=target,
        text=text, max_chars=max_chars, today="2026-07-02",
        history_path=hist, turn=turn, external_ctx=external_ctx)


def test_add_logs_pin_add_with_turn_and_ctx(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    _apply(tmp_path, hist, op="add", text="喜歡看監控數字", turn=7, external_ctx=True)
    (ev,) = _events(hist)
    assert ev["kind"] == "pin_add" and ev["turn"] == 7 and ev["external_ctx"] is True
    assert ev["section"] == "self" and ev["id"] == "s1" and ev["text"] == "喜歡看監控數字"


def test_replace_logs_tombstone_old_text(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    _apply(tmp_path, hist, op="add", text="舊的我", turn=1)
    _apply(tmp_path, hist, op="replace", target="s1", text="新的我", turn=2)
    add, rep = _events(hist)
    assert rep["kind"] == "pin_replace" and rep["old_text"] == "舊的我" and rep["text"] == "新的我"


def test_remove_logs_tombstone(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    _apply(tmp_path, hist, op="add", text="被淘汰的自我", turn=1)
    _apply(tmp_path, hist, op="remove", target="s1", turn=3)
    _, rm = _events(hist)
    assert rm["kind"] == "pin_remove" and rm["old_text"] == "被淘汰的自我"


def test_same_turn_dedup_hit_logs_nothing(tmp_path):
    """Refeed artifact: re-emitted identical pin in the SAME outer turn."""
    hist = tmp_path / "self_history.jsonl"
    _apply(tmp_path, hist, op="add", text="A", turn=5)
    result = _apply(tmp_path, hist, op="add", text="A", turn=5)
    assert "已有相同條目" in result
    assert [e["kind"] for e in _events(hist)] == ["pin_add"]  # no reconfirm


def test_cross_turn_dedup_hit_logs_reconfirm(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    _apply(tmp_path, hist, op="add", text="A", turn=5)
    _apply(tmp_path, hist, op="add", text="A", turn=35)
    kinds = [e["kind"] for e in _events(hist)]
    assert kinds == ["pin_add", "pin_reconfirm"]
    assert _events(hist)[1]["turn"] == 35


def test_reconfirm_chain_is_cross_turn_per_last_event(tmp_path):
    """Second reconfirm in the same turn as the first reconfirm → not logged."""
    hist = tmp_path / "self_history.jsonl"
    _apply(tmp_path, hist, op="add", text="A", turn=5)
    _apply(tmp_path, hist, op="add", text="A", turn=35)
    _apply(tmp_path, hist, op="add", text="A", turn=35)  # refeed echo of the reconfirm turn
    assert [e["kind"] for e in _events(hist)] == ["pin_add", "pin_reconfirm"]


def test_cap_rejected_add_logs_nothing(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    result = _apply(tmp_path, hist, op="add", text="X" * 100, max_chars=30)
    assert "已達上限" in result
    assert _events(hist) == []


def test_history_path_none_is_backcompat_noop(tmp_path):
    result = self_profile.apply(
        tmp_path / "self_profile.md", section="self", op="add", target="",
        text="A", max_chars=1200, today="2026-07-02")
    assert "已 pin" in result


def test_history_read_error_swallowed_dedup_result_unchanged(tmp_path, monkeypatch):
    hist = tmp_path / "self_history.jsonl"
    _apply(tmp_path, hist, op="add", text="A", turn=5)

    def boom(path, **kw):
        raise OSError("perm denied")
    monkeypatch.setattr(self_history, "last_pin_turn", boom)
    result = _apply(tmp_path, hist, op="add", text="A", turn=35)
    assert "已有相同條目" in result           # pin contract survives
    assert len(_events(hist)) == 1            # no reconfirm logged


def test_log_io_error_swallowed_pin_still_succeeds(tmp_path, monkeypatch):
    def boom(path, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(self_history, "log_event", boom)
    hist = tmp_path / "self_history.jsonl"
    result = _apply(tmp_path, hist, op="add", text="A")
    assert "已 pin" in result  # friendly result unchanged
    assert (tmp_path / "self_profile.md").exists()  # the pin itself landed
