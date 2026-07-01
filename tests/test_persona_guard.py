"""Tests for check_persona_violations (spec 2026-07-01 persona-hardening §2)."""
from __future__ import annotations

from dollos.character import Enforcement
from dollos.mind.persona_guard import check_persona_violations


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
