"""Reciprocal Rank Fusion for hybrid retrieval scoring."""


def rrf_merge(
    vector_hits: list[tuple[int, float]],
    fts_hits: list[tuple[int, float]],
    k: int = 60,
) -> list[tuple[int, float]]:
    """Merge two ranked lists by Reciprocal Rank Fusion.

    Args:
        vector_hits: list of (fact_id, distance) ordered by ascending distance
            (i.e. best first).
        fts_hits: list of (fact_id, score) ordered best first.
        k: RRF constant. Default 60 is the industry-standard value.

    Returns:
        list of (fact_id, score) ordered by descending score.

    The score for a fact is the sum over both lists of 1 / (k + rank + 1),
    where rank is the 0-based position in each list. Facts appearing in
    only one list contribute one term.
    """
    scores: dict[int, float] = {}
    for rank, (fact_id, _) in enumerate(vector_hits):
        scores[fact_id] = scores.get(fact_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, (fact_id, _) in enumerate(fts_hits):
        scores[fact_id] = scores.get(fact_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda p: -p[1])
