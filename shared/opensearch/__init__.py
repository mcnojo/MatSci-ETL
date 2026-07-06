"""OpenSearch client + index-write path for the chunk store.

Write-side only: this repo owns chunking, embedding, and bulk-loading the
BM25 + kNN index. Retrieval (BM25 / RRF / rerank) lives in the downstream
agent — see docs/vector-and-bm25-access.md.
"""

from .client import build_client, resolve_endpoint
from .mapping import CHUNK_INDEX_MAPPING, chunk_index_mapping, ensure_index
from .writer import bulk_index_chunks, wipe_paper

__all__ = [
    "build_client",
    "resolve_endpoint",
    "CHUNK_INDEX_MAPPING",
    "chunk_index_mapping",
    "ensure_index",
    "bulk_index_chunks",
    "wipe_paper",
]
