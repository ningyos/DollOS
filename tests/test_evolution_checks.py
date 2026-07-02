"""Echo-equivalence + mechanical checks + shared pairwise Jaccard (spec §3.1/§3.3/§3.4)."""
from dollos.character import Enforcement
from dollos.mind import evolution as evo
from dollos.mind.persona_guard import pairwise_jaccard, response_drift_score


def test_pairwise_jaccard_identical_is_one():
    assert pairwise_jaccard("我喜歡監控數字", "我喜歡監控數字") == 1.0


def test_pairwise_jaccard_disjoint_is_zero():
    assert pairwise_jaccard("蘋果", "橘子") == 0.0


def test_pairwise_jaccard_both_empty_is_one():
    assert pairwise_jaccard("", "") == 1.0


def test_response_drift_score_still_works():
    # Refactor must not change existing behavior.
    assert response_drift_score("A", []) == 1.0


def test_echo_equivalent_exact_after_normalize():
    assert evo.echo_equivalent("  我 現在的樣子。 ", "我現在的樣子。") is True


def test_echo_equivalent_paraphrase_above_threshold():
    ref = "我現在監控數字跳動時會主動來勁,不再只是安靜待著。"
    para = "我現在監控數字跳動時會主動來勁,不再安靜待著。"
    assert evo.echo_equivalent(para, ref) is True


def test_echo_equivalent_genuinely_different_is_false():
    assert evo.echo_equivalent("我其實喜歡園藝跟做菜。", "我現在監控數字會來勁。") is False


def test_echo_equivalent_strips_surfacing_markers():
    from dollos.mind import surfacing_markers as sm
    surfaced = f"{sm.NEW} 我現在的樣子。"
    assert evo.echo_equivalent(surfaced, "我現在的樣子。") is True


def test_mechanical_checks_floor():
    reason = evo.mechanical_checks("太短", floor=80, cap=600, enforcement=Enforcement())
    assert reason is not None and "80" in reason


def test_mechanical_checks_cap():
    reason = evo.mechanical_checks("字" * 601, floor=80, cap=600, enforcement=Enforcement())
    assert reason is not None and "600" in reason


def test_mechanical_checks_banned_substring():
    enf = Enforcement(banned_substrings=["LARP"])
    reason = evo.mechanical_checks("我" * 80 + "LARP", floor=80, cap=600, enforcement=enf)
    assert reason is not None and "LARP" in reason


def test_mechanical_checks_clean_returns_none():
    assert evo.mechanical_checks("我" * 100, floor=80, cap=600, enforcement=Enforcement()) is None
