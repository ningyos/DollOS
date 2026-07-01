"""Tests for detect_repeat_streak (spec §13.1 P6, deterministic successful-repeat detector)."""
from __future__ import annotations

from dollos.mind.mind_state import OutputRecord
from dollos.mind.repeat_detect import REPEAT_STREAK_THRESHOLD, detect_repeat_streak


def _rec(kind: str, summary: str, t: float = 0.0) -> OutputRecord:
    return OutputRecord(t=t, kind=kind, summary=summary)


def test_empty_outputs_returns_none():
    assert detect_repeat_streak([]) is None


def test_below_threshold_returns_none():
    outs = [_rec("SetFocus", "focus → x"), _rec("SetFocus", "focus → x")]
    assert detect_repeat_streak(outs) is None


def test_at_threshold_trips():
    outs = [_rec("SetFocus", "focus → x")] * REPEAT_STREAK_THRESHOLD
    assert detect_repeat_streak(outs) == ("SetFocus", "focus → x", 3)


def test_above_threshold_reports_full_count():
    outs = [_rec("SetFocus", "focus → x")] * 5
    assert detect_repeat_streak(outs) == ("SetFocus", "focus → x", 5)


def test_mixed_tail_not_uniform_does_not_false_positive():
    """A streak buried earlier, broken by a different trailing entry, must not trip."""
    outs = [
        _rec("SetFocus", "focus → x"),
        _rec("SetFocus", "focus → x"),
        _rec("SetFocus", "focus → x"),
        _rec("Recall", "recalled: y"),
    ]
    assert detect_repeat_streak(outs) is None


def test_same_kind_different_summary_does_not_count():
    outs = [
        _rec("SetFocus", "focus → x"),
        _rec("SetFocus", "focus → x"),
        _rec("SetFocus", "focus → y"),
    ]
    assert detect_repeat_streak(outs) is None


def test_speech_excluded_even_at_high_count():
    outs = [_rec("Speech", "spoke: hi")] * 10
    assert detect_repeat_streak(outs) is None


def test_custom_threshold():
    outs = [_rec("Shell", "ran: ls")] * 2
    assert detect_repeat_streak(outs, threshold=2) == ("Shell", "ran: ls", 2)


def test_non_uniform_short_history_returns_none():
    outs = [_rec("Recall", "recalled: a"), _rec("NoteMemory", "noted: b")]
    assert detect_repeat_streak(outs) is None
