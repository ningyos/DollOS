"""Mode A pure helpers (spec §3.3: material gate / interval dynamics / HWM)."""
import time

from dollos.mind import evolution as evo, self_history


def _seed(hist, kinds):
    for k in kinds:
        self_history.log_event(hist, kind=k, turn=1, external_ctx=False,
                               section="self", id="s1", text="A")


def test_count_new_pin_events_counts_pins_only_past_hwm(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    _seed(hist, ["pin_add", "pin_replace"])
    hwm = hist.stat().st_size
    _seed(hist, ["pin_add", "pin_reconfirm", "pin_remove"])
    self_history.log_event(hist, kind="evo_no_change")   # bookkeeping — never counts
    self_history.log_event(hist, kind="evo_adopt", text="X" * 90,
                           old_text=None, drift_score=None)
    assert evo.count_new_pin_events(hist, hwm) == 3
    assert evo.count_new_pin_events(hist, 0) == 5
    assert evo.count_new_pin_events(tmp_path / "nope.jsonl", 0) == 0


def test_count_tolerates_torn_tail(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    _seed(hist, ["pin_add"])
    with hist.open("a", encoding="utf-8") as f:
        f.write('{"kind": "pin_add"')
    assert evo.count_new_pin_events(hist, 0) == 1


def test_diary_days_since_counts_dated_files_with_diary_heading(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "2026-06-20.md").write_text("## x 日記\n內容", encoding="utf-8")
    (shared / "2026-06-25.md").write_text("## x 日記\n內容", encoding="utf-8")
    (shared / "2026-06-26.md").write_text("純筆記,沒有日記段", encoding="utf-8")
    (shared / "notafile.md").write_text("## 日記", encoding="utf-8")  # non-date stem
    since = time.mktime((2026, 6, 22, 0, 0, 0, 0, 0, -1))
    assert evo.diary_days_since(shared, since) == 1        # only 06-25 qualifies
    assert evo.diary_days_since(shared, 0.0) == 2          # 06-20 + 06-25
    assert evo.diary_days_since(tmp_path / "none", 0.0) == 0


def test_has_diary_heading_is_heading_anchored(tmp_path):
    """F7: the shared predicate matches a ## … 日記 heading, NOT a bare 日記
    substring — so a note that merely mentions 日記 in prose is not counted."""
    assert evo.has_diary_heading("## 深夜 日記\n今天很平靜") is True
    assert evo.has_diary_heading("## 日記") is True
    assert evo.has_diary_heading("我今天寫了日記,但這是純筆記") is False
    assert evo.has_diary_heading("") is False


def test_next_interval_days_table():
    assert evo.next_interval_days(7.0, outcome="evo_adopt", base=7.0, cap=28.0) == 7.0
    assert evo.next_interval_days(7.0, outcome="evo_no_change", base=7.0, cap=28.0) == 14.0
    assert evo.next_interval_days(14.0, outcome="evo_kill", base=7.0, cap=28.0) == 28.0
    assert evo.next_interval_days(28.0, outcome="evo_reject", base=7.0, cap=28.0) == 28.0
    assert evo.next_interval_days(14.0, outcome="evo_expire", base=7.0, cap=28.0) == 14.0


def test_history_snapshot_renders_tail_and_returns_offset(tmp_path):
    hist = tmp_path / "self_history.jsonl"
    _seed(hist, ["pin_add"])
    hwm = hist.stat().st_size
    self_history.log_event(hist, kind="pin_remove", turn=9, external_ctx=True,
                           section="self", id="s1", text="", old_text="舊的我")
    text, new_off = evo.history_snapshot(hist, hwm)
    assert "pin_remove" in text and "舊的我" in text and "external_ctx" in text
    assert "pin_add" not in text                      # before hwm — excluded
    assert new_off == hist.stat().st_size
    empty, off0 = evo.history_snapshot(tmp_path / "nope.jsonl", 0)
    assert empty == "" and off0 == 0
