"""Collection provisioning.

`ensure_collection` is idempotent via `collection_exists`. New collections get
named vectors `dense` (cosine, dim from config) and sparse `bm25` (IDF-modified,
so Qdrant applies inverse-document-frequency scoring at query time). The
`paper_id` payload index is created BEFORE first ingest so the HNSW graph is
built filter-aware — post-ingest creation would leave the graph unindexed.
"""

from __future__ import annotations

from typing import Any


async def ensure_collection(client: Any, name: str, embedding_dim: int) -> None:
    from qdrant_client import models        # deferred: heavy import

    if await client.collection_exists(collection_name=name):
        return
    await client.create_collection(
        collection_name=name,
        vectors_config={
            "dense": models.VectorParams(
                size=embedding_dim,
                distance=models.Distance.COSINE,
            ),
        },
        sparse_vectors_config={
            "bm25": models.SparseVectorParams(modifier=models.Modifier.IDF),
        },
    )
    await client.create_payload_index(
        collection_name=name,
        field_name="paper_id",
        field_schema=models.PayloadSchemaType.KEYWORD,
    )
