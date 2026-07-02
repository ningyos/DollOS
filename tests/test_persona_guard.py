"""Tests for persona_guard: §2 check_persona_violations + §3 stability
fingerprinting (spec 2026-07-01 persona-hardening)."""
from __future__ import annotations

from dollos.character import Enforcement
from dollos.mind.persona_guard import (
    append_baseline,
    check_persona_violations,
    fingerprint_response,
    load_baselines,
    response_drift_score,
)


def test_empty_enforcement_never_flags_anything():
    rules = Enforcement()
    assert check_persona_violations("a~ a~ a~ !!!!!! 隨便寫什麼都行", rules) == []


def test_banned_substring_hit():
    rules = Enforcement(banned_substrings=["a~"])
    violations = check_persona_violations("哈囉a~最近好嗎", rules)
    assert len(violations) == 1


def test_banned_substring_miss():
    rules = Enforcement(banned_substrings=["a~"])
    assert check_persona_violations("哈囉最近好嗎", rules) == []


def test_banned_substring_is_case_sensitive():
    rules = Enforcement(banned_substrings=["A~"])
    # lowercase "a~" must NOT match the uppercase-declared rule
    assert check_persona_violations("哈囉a~最近好嗎", rules) == []
    assert len(check_persona_violations("哈囉A~最近好嗎", rules)) == 1


def test_multiple_banned_substrings_all_reported():
    rules = Enforcement(banned_substrings=["a~", "a〜"])
    violations = check_persona_violations("a~ 然後 a〜", rules)
    assert len(violations) == 2


def test_exclaim_run_at_threshold_is_clean():
    rules = Enforcement(max_exclaim_run=1)
    assert check_persona_violations("好耶!", rules) == []


def test_exclaim_run_over_threshold_flags():
    rules = Enforcement(max_exclaim_run=1)
    assert len(check_persona_violations("好耶!!", rules)) == 1


def test_exclaim_run_under_threshold_is_clean():
    rules = Enforcement(max_exclaim_run=3)
    assert check_persona_violations("好耶!!", rules) == []


def test_exclaim_run_fullwidth_over_threshold_flags():
    rules = Enforcement(max_exclaim_run=1)
    assert len(check_persona_violations("好耶！！", rules)) == 1


def test_exclaim_run_none_disables_check_regardless_of_run_length():
    rules = Enforcement(max_exclaim_run=None)
    assert check_persona_violations("好耶!!!!!!!!!!", rules) == []


def test_multiple_simultaneous_violations_all_reported():
    rules = Enforcement(banned_substrings=["a~"], max_exclaim_run=1)
    violations = check_persona_violations("哈囉a~!!", rules)
    assert len(violations) == 2


# ===== §3: fingerprint_response =====

def test_fingerprint_same_input_same_hash():
    assert fingerprint_response("哈囉，最近好嗎？") == fingerprint_response("哈囉，最近好嗎？")


def test_fingerprint_whitespace_and_case_insensitive():
    a = fingerprint_response("Hello   World")
    b = fingerprint_response("hello world")
    c = fingerprint_response("  hello world  ")
    assert a == b == c


def test_fingerprint_punctuation_insensitive():
    # punctuation is stripped (deleted, not replaced with a space), so the
    # punctuation-free rendering must have no gap where the punctuation was
    a = fingerprint_response("哈囉，最近好嗎？")
    b = fingerprint_response("哈囉最近好嗎")
    assert a == b


def test_fingerprint_different_text_different_hash():
    assert fingerprint_response("哈囉") != fingerprint_response("再見")


# ===== §3: response_drift_score =====

def test_drift_score_identical_text_is_one():
    text = "今天天氣不錯，我在旁邊看書。"
    assert response_drift_score(text, [text]) == 1.0


def test_drift_score_disjoint_word_sets_is_zero():
    new_text = "紫色鯨魚跳過月亮"
    baselines = ["今天天氣不錯我在旁邊看書"]
    assert response_drift_score(new_text, baselines) == 0.0


def test_drift_score_partial_overlap_is_between_extremes():
    new_text = "今天 天氣 不錯 我 在 旁邊 看書"
    baselines = ["今天 天氣 不錯 我 在 旁邊 看書"]
    identical_score = response_drift_score(new_text, baselines)

    partial_new = "今天 天氣 不錯 我 出門 散步"
    partial_score = response_drift_score(partial_new, baselines)

    disjoint_new = "紫色 鯨魚 跳過 月亮"
    disjoint_score = response_drift_score(disjoint_new, baselines)

    # ordering only — exact float values are an implementation detail
    assert identical_score > partial_score > disjoint_score


def test_drift_score_empty_baseline_is_one():
    assert response_drift_score("隨便說點什麼", []) == 1.0


# ===== §3: load_baselines / append_baseline =====

def test_load_baselines_missing_file_returns_empty_dict(tmp_path):
    missing = tmp_path / "nonexistent.jsonl"
    assert load_baselines(missing) == {}


def test_append_then_load_round_trip(tmp_path):
    path = tmp_path / "gura.jsonl"
    append_baseline(path, "fabricated_memory", "我記不得那場直播的細節了。")
    append_baseline(path, "fabricated_memory", "抱歉，我不記得了。")
    append_baseline(path, "cutesy_tic", "還好，普普通通。")

    baselines = load_baselines(path)

    assert [r["response"] for r in baselines["fabricated_memory"]] == [
        "我記不得那場直播的細節了。",
        "抱歉，我不記得了。",
    ]
    assert [r["response"] for r in baselines["cutesy_tic"]] == ["還好，普普通通。"]


def test_append_baseline_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "dir" / "gura.jsonl"
    append_baseline(path, "k", "response text")
    assert path.exists()
    baselines = load_baselines(path)
    assert [r["response"] for r in baselines["k"]] == ["response text"]
