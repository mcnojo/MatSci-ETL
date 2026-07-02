"""OpenSearch client + hybrid-search wrapper for chunk retrieval.

Backend for the BM25 / hybrid-RAG comparison route. Client is opensearch-py
(drop-in with elasticsearch-py); the same code targets self-hosted OpenSearch
on EC2 or Amazon OpenSearch Service. Managed OS uses SigV4 auth via
requests-aws4auth; self-hosted uses basic auth.
"""

from .client import build_client, resolve_endpoint
from .mapping import CHUNK_INDEX_MAPPING, ensure_index
from .query import HitResult, hybrid_search, rrf_merge
from .writer import bulk_index_chunks, wipe_paper
