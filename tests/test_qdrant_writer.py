"""Unit tests for shared.qdrant.writer — wipe + upsert semantics.

Fakes AsyncQdrantClient with just the three methods the writer touches. Sync
test bodies drive async writer calls via asyncio.run so pytest picks them up
without pytest-asyncio.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import numpy as np

from shared.qdrant.writer import bulk_index_chunks, wipe_paper
from shared.schemas import Chunk


@dataclass
class _SparseVec:
    """Duck-typed stand-in for fastembed.SparseEmbedding."""
    indices: Any
    values: Any


@dataclass
class _FakeClient:
    exists: bool = True
    delete_calls: list[dict] = field(default_factory=list)
    upsert_calls: list[dict] = field(default_factory=list)

    async def collection_exists(self, *, collection_name: str) -> bool:
        return self.exists

    async def delete(self, *, collection_name, points_selector, wait) -> None:
        self.delete_calls.append({
            "collection_name": collection_name,
            "points_selector": points_selector,
            "wait": wait,
        })

    async def upsert(self, *, collection_name, points, wait) -> None:
        self.upsert_calls.append({
            "collection_name": collection_name,
            "points": list(points),
            "wait": wait,
        })


def _chunk(doc_id: str, text: str = "hello") -> Chunk:
    return Chunk(
        doc_id=doc_id, paper_id="p1", node_id="n1", sub_index=0,
        node_title="t", page_start=1, page_end=1,
        kind="section_text", source_kind="pymupdf",
        text=text, token_count=1,
    )


def test_wipe_paper_noop_when_collection_missing():
    c = _FakeClient(exists=False)
    asyncio.run(wipe_paper(c, "chunks-v1", "paper-x"))
    assert c.delete_calls == []


def test_wipe_paper_filters_by_paper_id_wait_true():
    from qdrant_client import models
    c = _FakeClient(exists=True)
    asyncio.run(wipe_paper(c, "chunks-v1", "paper-x"))
    assert len(c.delete_calls) == 1
    call = c.delete_calls[0]
    assert call["collection_name"] == "chunks-v1"
    assert call["wait"] is True
    sel = call["points_selector"]
    assert isinstance(sel, models.FilterSelector)
    cond = sel.filter.must[0]
    assert cond.key == "paper_id"
    assert cond.match.value == "paper-x"


def test_bulk_index_deterministic_uuid_and_vector_shape():
    from qdrant_client import models
    c = _FakeClient()
    chunks = [_chunk("p1:n1:0"), _chunk("p1:n1:1")]
    dense = [[0.1] * 4, [0.2] * 4]
    sparse = [
        _SparseVec(indices=np.array([1, 5]), values=np.array([0.7, 0.3])),
        _SparseVec(indices=np.array([2]),    values=np.array([1.0])),
    ]
    n = asyncio.run(bulk_index_chunks(c, "chunks-v1", chunks, dense, sparse))
    assert n == 2
    pts = c.upsert_calls[0]["points"]
    assert pts[0].id == str(uuid5(NAMESPACE_URL, "p1:n1:0"))
    assert pts[1].id == str(uuid5(NAMESPACE_URL, "p1:n1:1"))
    assert pts[0].vector["dense"] == [0.1] * 4
    bm = pts[0].vector["bm25"]
    assert isinstance(bm, models.SparseVector)
    assert bm.indices == [1, 5] and bm.values == [0.7, 0.3]
    assert "embedding" not in pts[0].payload            # vector never duplicated in payload
    assert pts[0].payload["doc_id"] == "p1:n1:0"
    assert c.upsert_calls[0]["wait"] is True


def test_bulk_index_empty_returns_zero():
    c = _FakeClient()
    n = asyncio.run(bulk_index_chunks(c, "chunks-v1", [], [], []))
    assert n == 0
    assert c.upsert_calls == []


def test_bulk_index_length_mismatch_raises():
    c = _FakeClient()
    try:
        asyncio.run(bulk_index_chunks(c, "chunks-v1", [_chunk("a")], [], []))
    except ValueError:
        return
    raise AssertionError("expected ValueError on length mismatch")


def _run_all() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    return len(tests)


if __name__ == "__main__":
    n = _run_all()
    print(f"PASS: {n} qdrant writer tests")
