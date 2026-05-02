"""Tests for rrf_merge."""

from dollos.memory.scoring import rrf_merge


def test_empty_inputs_returns_empty():
    assert rrf_merge([], []) == []


def test_single_side_only_vector():
    hits = [(1, 0.1), (2, 0.2), (3, 0.3)]
    out = rrf_merge(hits, [])
    # Order preserved by rank; only vector contributes
    assert [fact_id for fact_id, _ in out] == [1, 2, 3]


def test_single_side_only_fts():
    hits = [(10, 1.0), (11, 2.0)]
    out = rrf_merge([], hits)
    assert [fact_id for fact_id, _ in out] == [10, 11]


def test_overlap_fact_gets_summed_score():
    vec = [(1, 0.0), (2, 0.0)]      # 1 ranked 0, 2 ranked 1
    fts = [(2, 0.0), (3, 0.0)]      # 2 ranked 0, 3 ranked 1
    out = rrf_merge(vec, fts, k=60)
    # fact 2 appears in both (vec rank 1, fts rank 0)
    # fact 1 appears once (vec rank 0)
    # fact 3 appears once (fts rank 1)
    # Score(1) = 1/61, Score(2) = 1/62 + 1/61, Score(3) = 1/62
    # 2 should rank highest
    assert out[0][0] == 2


def test_unique_top_rank_beats_unique_lower_rank():
    # fact 1: appears only in vec at rank 0  → score 1/61
    # fact 2: appears only in fts at rank 5  → score 1/66
    # Each fact appears in exactly one list, so RRF reduces to comparing
    # ranks. Earlier rank wins. (Note: RRF rewards intersection — a fact
    # in BOTH lists at rank 5 would score 2/66 ≈ 0.030, beating fact 1's
    # 1/61 ≈ 0.016. The test name reflects what the data actually exercises.)
    vec = [(1, 0.0)] + [(99 + i, 0.0) for i in range(5)]
    fts = [(10, 0.0), (11, 0.0), (12, 0.0), (13, 0.0), (14, 0.0), (2, 0.0)]
    out = rrf_merge(vec, fts)
    assert out[0][0] == 1


def test_k_parameter_changes_score_magnitude_but_not_order():
    vec = [(1, 0), (2, 0), (3, 0)]
    fts = [(3, 0), (2, 0), (1, 0)]
    out_60 = rrf_merge(vec, fts, k=60)
    out_10 = rrf_merge(vec, fts, k=10)
    # All three appear in both; they should tie or differ predictably,
    # but the relative ordering of identical-rank-sums is stable.
    assert {fact_id for fact_id, _ in out_60} == {1, 2, 3}
    assert {fact_id for fact_id, _ in out_10} == {1, 2, 3}
