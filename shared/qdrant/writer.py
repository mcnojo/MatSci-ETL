"""Upsert + wipe. Both use `wait=True` — Qdrant is eventually consistent by
default and `wipe_paper → bulk_index_chunks` would race without it.

Point IDs are `uuid5(NAMESPACE_URL, chunk.doc_id)`: deterministic so retries
overwrite in place, UUID-typed so Qdrant accepts them (strings are rejected
unless they parse as UUID). Original doc_id stays in the payload for human
traceback.
"""

from __future__ import annotations

from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5

from shared.schemas import Chunk


async def bulk_index_chunks(
    client: Any,
    collection_name: str,
    chunks: list[Chunk],
    dense_vecs: list[list[float]],
    sparse_vecs: list[Any],       # list[fastembed.SparseEmbedding]
    *,
    batch_size: int = 256,
) -> int:
    """Upsert `chunks` with paired dense + sparse vectors. Returns the count
    persisted. Fails loudly on length mismatch — caller must guarantee parity.
    """
    from qdrant_client import models        # deferred: heavy import

    if not (len(chunks) == len(dense_vecs) == len(sparse_vecs)):
        raise ValueError(
            f"length mismatch: chunks={len(chunks)}, dense={len(dense_vecs)}, "
            f"sparse={len(sparse_vecs)}"
        )
    if not chunks:
        return 0

    points: list[Any] = []
    for c, dense, sparse in zip(chunks, dense_vecs, sparse_vecs):
        payload = c.model_dump(exclude_none=True)
        payload["breadcrumb_text"] = " > ".join(c.breadcrumb) if c.breadcrumb else ""
        payload.pop("embedding", None)        # vector lives outside payload
        points.append(models.PointStruct(
            id=str(uuid5(NAMESPACE_URL, c.doc_id)),
            vector={
                "dense": dense,
                "bm25": models.SparseVector(
                    indices=sparse.indices.tolist(),
                    values=sparse.values.tolist(),
                ),
            },
            payload=payload,
        ))

    total = 0
    for start in range(0, len(points), batch_size):
        batch = points[start:start + batch_size]
        await client.upsert(
            collection_name=collection_name,
            points=batch,
            wait=True,
        )
        total += len(batch)
    return total


async def wipe_paper(client: Any, collection_name: str, paper_id: str) -> None:
    """Delete every point tagged `paper_id`. No-op when the collection is
    missing (ensure_collection materializes it on the first write).
    """
    from qdrant_client import models        # deferred: heavy import

    if not await client.collection_exists(collection_name=collection_name):
        return
    await client.delete(
        collection_name=collection_name,
        points_selector=models.FilterSelector(
            filter=models.Filter(must=[
                models.FieldCondition(
                    key="paper_id",
                    match=models.MatchValue(value=paper_id),
                ),
            ]),
        ),
        wait=True,
    )
