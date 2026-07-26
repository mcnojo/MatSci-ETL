"""Qdrant Cloud write path — hybrid (dense + BM25 sparse) chunk store.

Named vectors: `dense` (1024-d, cosine) + `bm25` (sparse, IDF-modified).
Retrieval-side hybrid fuses both legs with server-side RRF; downstream owns
retrieval (see docs/vector-and-bm25-access.md).
"""

from .bm25 import encode_bm25
from .client import build_client
from .mapping import ensure_collection
from .writer import bulk_index_chunks, wipe_paper

__all__ = [
    "build_client",
    "ensure_collection",
    "bulk_index_chunks",
    "wipe_paper",
    "encode_bm25",
]
