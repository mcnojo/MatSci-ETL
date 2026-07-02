"""Chunk index mapping — BM25 text + Lucene HNSW k-NN in one index.

Lucene engine (not FAISS/nmslib) keeps the runtime dep-light and matches the
default on modern OpenSearch. `dimension` is stamped from config at
ensure_index time so an embedding-model swap requires a new index name
(no silent mismatches).
"""

from __future__ import annotations

from typing import Any


CHUNK_INDEX_SETTINGS = {
    "index": {
        "knn": True,
        "knn.algo_param.ef_search": 100,
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "analysis": {
        "analyzer": {
            "default": {"type": "english"},
        },
    },
}


def chunk_index_mapping(embedding_dim: int) -> dict[str, Any]:
    """Return the mapping body for `ensure_index`, dim baked in."""
    return {
        "properties": {
            "doc_id": {"type": "keyword"},
            "paper_id": {"type": "keyword"},
            "node_id": {"type": "keyword"},
            "sub_index": {"type": "integer"},
            "node_title": {"type": "text"},
            "breadcrumb": {"type": "keyword"},         # exact filter (per-crumb)
            "breadcrumb_text": {"type": "text"},       # joined for match queries
            "depth": {"type": "integer"},
            "position": {"type": "integer"},
            "page_start": {"type": "integer"},
            "page_end": {"type": "integer"},
            "kind": {"type": "keyword"},
            "source_kind": {"type": "keyword"},
            "text": {"type": "text"},
            "token_count": {"type": "integer"},
            "tree_uri": {"type": "keyword"},
            "chunker_version": {"type": "keyword"},
            "embedding": {
                "type": "knn_vector",
                "dimension": embedding_dim,
                "method": {
                    "name": "hnsw",
                    "space_type": "cosinesimil",
                    "engine": "lucene",
                    "parameters": {"ef_construction": 128, "m": 16},
                },
            },
        }
    }


# Public alias — most callers just need the settings + a dim.
CHUNK_INDEX_MAPPING = {
    "settings": CHUNK_INDEX_SETTINGS,
    # mappings filled by ensure_index()
}


def ensure_index(client: Any, index_name: str, embedding_dim: int) -> None:
    """Create the index if it doesn't exist.

    Doesn't diff-mutate an existing index — OpenSearch doesn't allow live
    mapping changes for knn_vector dim. To migrate, rotate index names.
    """
    if client.indices.exists(index=index_name):
        return
    body = {
        "settings": CHUNK_INDEX_SETTINGS,
        "mappings": chunk_index_mapping(embedding_dim),
    }
    client.indices.create(index=index_name, body=body)
