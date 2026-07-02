"""Hybrid retrieval: parallel BM25 + k-NN, fused with Reciprocal Rank Fusion.

RRF is done client-side in Python (not OpenSearch's built-in `hybrid` query)
so the fusion is portable, testable in isolation, and doesn't rely on
server-side pipeline config. Downstream can pass the top-k to a reranker
(cross-encoder / listwise LLM) without further plumbing.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class HitResult(BaseModel):
    doc_id: str
    score: float                         # RRF score (fused) OR raw BM25/k-NN score
    source: dict                         # _source payload from OpenSearch
    bm25_rank: Optional[int] = None      # 1-based; None if absent from BM25 hits
    knn_rank: Optional[int] = None       # 1-based; None if absent from k-NN hits


def hybrid_search(
    client: Any,
    index: str,
    *,
    query: str,
    embedding: list[float],
    k: int = 50,
    per_leg_k: int = 50,
    rrf_k: int = 60,
    filters: Optional[dict] = None,
) -> list[HitResult]:
    """Run BM25 + k-NN in parallel; return the top-k RRF fusion.

    filters: opensearch bool-filter clauses appended to both legs, e.g.
        {"term": {"paper_id": "..."}} or {"terms": {"kind": ["section_text"]}}.
    per_leg_k: how many candidates each leg produces before fusion. Should be
        ≥ final k; larger widens the fusion pool at query cost.
    rrf_k: RRF smoothing constant. 60 is the paper default.
    """
    bm25_body = _bm25_body(query, per_leg_k, filters)
    knn_body = _knn_body(embedding, per_leg_k, filters)

    # OpenSearch doesn't ship an async client in the general Python lib; the two
    # calls run sequentially. Both are millisecond-scale, so serial is fine.
    bm25_hits = client.search(index=index, body=bm25_body)["hits"]["hits"]
    knn_hits = client.search(index=index, body=knn_body)["hits"]["hits"]

    return rrf_merge(bm25_hits, knn_hits, k=k, rrf_k=rrf_k)


def rrf_merge(
    bm25_hits: list[dict],
    knn_hits: list[dict],
    *,
    k: int,
    rrf_k: int = 60,
) -> list[HitResult]:
    """Reciprocal Rank Fusion.

    score(d) = sum_over_legs 1 / (rrf_k + rank_leg(d))

    Docs present in only one leg still get a contribution from that leg. Ties
    break by higher raw BM25 score (arbitrary but stable).
    """
    fused: dict[str, HitResult] = {}
    _accumulate(fused, bm25_hits, leg="bm25", rrf_k=rrf_k)
    _accumulate(fused, knn_hits, leg="knn", rrf_k=rrf_k)
    ordered = sorted(
        fused.values(),
        key=lambda h: (h.score, h.source.get("_bm25_raw", 0.0)),
        reverse=True,
    )
    return ordered[:k]


def _accumulate(
    fused: dict[str, HitResult],
    hits: list[dict],
    *,
    leg: str,
    rrf_k: int,
) -> None:
    for rank, hit in enumerate(hits, start=1):
        doc_id = _hit_doc_id(hit)
        contribution = 1.0 / (rrf_k + rank)
        existing = fused.get(doc_id)
        if existing is None:
            source = dict(hit.get("_source", {}))
            if leg == "bm25":
                source["_bm25_raw"] = hit.get("_score", 0.0)
            fused[doc_id] = HitResult(
                doc_id=doc_id,
                score=contribution,
                source=source,
                bm25_rank=rank if leg == "bm25" else None,
                knn_rank=rank if leg == "knn" else None,
            )
            continue
        # Pydantic model is frozen-adjacent; rebuild with updated fields.
        fused[doc_id] = existing.model_copy(update={
            "score": existing.score + contribution,
            "bm25_rank": rank if leg == "bm25" else existing.bm25_rank,
            "knn_rank": rank if leg == "knn" else existing.knn_rank,
        })


def _hit_doc_id(hit: dict) -> str:
    # Prefer the stored doc_id field so re-indexed docs collide by intent.
    return hit.get("_source", {}).get("doc_id") or hit["_id"]


def _bm25_body(query: str, size: int, filters: Optional[dict]) -> dict:
    q: dict = {"match": {"text": query}}
    return {"size": size, "query": _with_filters(q, filters)}


def _knn_body(embedding: list[float], size: int, filters: Optional[dict]) -> dict:
    q: dict = {"knn": {"embedding": {"vector": embedding, "k": size}}}
    return {"size": size, "query": _with_filters(q, filters)}


def _with_filters(inner: dict, filters: Optional[dict]) -> dict:
    if not filters:
        return inner
    clauses = filters if isinstance(filters, list) else [filters]
    return {"bool": {"must": [inner], "filter": clauses}}
