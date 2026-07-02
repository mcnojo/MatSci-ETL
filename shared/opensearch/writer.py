"""Bulk-index chunks + wipe previous versions of a paper.

`bulk_index_chunks` writes with deterministic doc_ids so a retry overwrites
in place. `wipe_paper` clears the paper's slot BEFORE writing when a
re-chunk produces fewer/renamed chunks than the last run, so the index never
carries stale rows from a superseded chunking strategy.
"""

from __future__ import annotations

from typing import Any, Iterable

from shared.schemas import Chunk


def bulk_index_chunks(
    client: Any,
    index: str,
    chunks: Iterable[Chunk],
    *,
    batch_size: int = 500,
) -> int:
    """Write chunks to `index`. Returns the count actually persisted.

    Uses opensearchpy.helpers.bulk with `_id = chunk.doc_id` so a second run
    overwrites the same rows (retries/reindexes stay idempotent). Chunks with
    embedding=None are indexed for BM25 only — the knn_vector field is nullable
    at the mapping level via `store` behavior (skipping the field is legal).
    """
    from opensearchpy.helpers import bulk  # deferred: heavy import

    def _iter_actions():
        for c in chunks:
            body = c.model_dump(exclude_none=True)
            body["breadcrumb_text"] = " > ".join(c.breadcrumb) if c.breadcrumb else ""
            yield {
                "_op_type": "index",
                "_index": index,
                "_id": c.doc_id,
                "_source": body,
            }

    ok, errors = bulk(
        client, _iter_actions(),
        chunk_size=batch_size,
        raise_on_error=True,
        request_timeout=60,
    )
    if errors:
        raise RuntimeError(f"bulk index failures: {errors[:3]}")
    return ok


def wipe_paper(client: Any, index: str, paper_id: str) -> int:
    """Delete every chunk for `paper_id` from `index`. Returns deleted count.

    Runs before `bulk_index_chunks` in the indexing activity so a re-run with
    a different chunk shape (fewer nodes, retuned splitter) can't leave
    orphans. `refresh=True` guarantees the deletes are visible before the
    subsequent bulk write; without it the writes race the deletes and can
    lose the new versions to lazy segment merges.

    Missing-index is not an error: ensure_index runs after this in the caller,
    which will materialize the mapping on the first write.
    """
    if client.indices.exists(index=index):
        resp = client.delete_by_query(
            index=index,
            body={"query": {"term": {"paper_id": paper_id}}},
            refresh=True,
        )
        return int(resp.get("deleted", 0))
    return 0
