"""FastEmbed BM25 sparse-vector encoder.

`Qdrant/bm25` ships a pretrained IDF table (general English). Model init is
~5s and holds ONNX runtime state; keep one instance per worker process.
"""

from __future__ import annotations

from typing import Any

_ENCODER: Any = None


def _get_encoder() -> Any:
    global _ENCODER
    if _ENCODER is None:
        from fastembed import SparseTextEmbedding        # deferred: heavy import
        _ENCODER = SparseTextEmbedding(model_name="Qdrant/bm25")
    return _ENCODER


def encode_bm25(texts: list[str]) -> list[Any]:
    """Return a list of SparseEmbedding objects (.indices / .values numpy arrays)
    aligned with `texts`. Blocking — call from a thread if the caller runs on
    an event loop.
    """
    return list(_get_encoder().embed(texts))
