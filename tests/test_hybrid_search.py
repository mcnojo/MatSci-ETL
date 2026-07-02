"""Unit tests for shared.opensearch.query — RRF fusion logic.

Verifies the fusion math and edge cases without touching a live OpenSearch.
Hits are shaped exactly like `_source`/`_score`/`_id` payloads from the client.
"""

from shared.opensearch.query import rrf_merge


def _hit(doc_id: str, score: float) -> dict:
    return {"_id": doc_id, "_score": score, "_source": {"doc_id": doc_id, "text": doc_id}}


# rrf_merge — fusion math

def test_doc_in_both_legs_scores_higher_than_solo():
    bm25 = [_hit("both", 5.0), _hit("bm_only", 3.0)]
    knn = [_hit("both", 0.9), _hit("kn_only", 0.7)]
    merged = rrf_merge(bm25, knn, k=10)
    scores = {h.doc_id: h.score for h in merged}
    assert scores["both"] > scores["bm_only"]
    assert scores["both"] > scores["kn_only"]


def test_rank_ordering_respected():
    # both legs return doc "a" first, doc "b" second — "a" > "b"
    bm25 = [_hit("a", 5.0), _hit("b", 4.0)]
    knn = [_hit("a", 0.9), _hit("b", 0.8)]
    merged = rrf_merge(bm25, knn, k=10)
    order = [h.doc_id for h in merged]
    assert order == ["a", "b"]


def test_solo_leg_docs_still_returned():
    bm25 = [_hit("only_bm25", 10.0)]
    knn = [_hit("only_knn", 0.5)]
    merged = rrf_merge(bm25, knn, k=10)
    doc_ids = {h.doc_id for h in merged}
    assert doc_ids == {"only_bm25", "only_knn"}


def test_rrf_score_formula():
    # rank-1 in both legs: score = 1/(60+1) + 1/(60+1) = 2/61
    bm25 = [_hit("x", 5.0)]
    knn = [_hit("x", 0.9)]
    merged = rrf_merge(bm25, knn, k=1, rrf_k=60)
    assert len(merged) == 1
    expected = 2.0 / 61.0
    assert abs(merged[0].score - expected) < 1e-9


def test_rrf_k_smoothing_moves_scores_but_not_order():
    bm25 = [_hit("a", 5.0), _hit("b", 4.0)]
    knn = [_hit("a", 0.9)]
    order_small_k = [h.doc_id for h in rrf_merge(bm25, knn, k=10, rrf_k=10)]
    order_big_k = [h.doc_id for h in rrf_merge(bm25, knn, k=10, rrf_k=1000)]
    assert order_small_k == order_big_k == ["a", "b"]


def test_top_k_truncation():
    bm25 = [_hit(f"d{i}", 10.0 - i) for i in range(20)]
    knn = [_hit(f"d{i}", 1.0 - i * 0.01) for i in range(20)]
    merged = rrf_merge(bm25, knn, k=5)
    assert len(merged) == 5


def test_ranks_carried_through():
    bm25 = [_hit("a", 5.0), _hit("b", 4.0), _hit("c", 3.0)]
    knn = [_hit("b", 0.9), _hit("d", 0.8)]
    merged = {h.doc_id: h for h in rrf_merge(bm25, knn, k=10)}
    assert merged["a"].bm25_rank == 1 and merged["a"].knn_rank is None
    assert merged["b"].bm25_rank == 2 and merged["b"].knn_rank == 1
    assert merged["c"].bm25_rank == 3 and merged["c"].knn_rank is None
    assert merged["d"].bm25_rank is None and merged["d"].knn_rank == 2


def test_empty_legs_produce_empty_result():
    assert rrf_merge([], [], k=10) == []


def test_one_empty_leg_yields_the_other():
    bm25 = [_hit("a", 5.0), _hit("b", 4.0)]
    merged = rrf_merge(bm25, [], k=10)
    assert [h.doc_id for h in merged] == ["a", "b"]


def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    return len(tests)


if __name__ == "__main__":
    n = _run_all()
    print(f"PASS: {n} hybrid_search tests")
