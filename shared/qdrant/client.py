"""AsyncQdrantClient factory.

URL + API key come from env vars named in `retrieval.qdrant.{url_env, api_key_env}`
(worker bootstrap materializes them from SSM). HTTP (port 6333) by default;
`prefer_grpc: true` flips to gRPC (6334) — Cloud free tier is HTTP-only.
"""

from __future__ import annotations

import os
from typing import Any


def build_client(config: dict) -> Any:
    from qdrant_client import AsyncQdrantClient        # deferred: heavy import

    q_cfg = config["retrieval"]["qdrant"]
    url = os.environ.get(q_cfg["url_env"], "")
    api_key = os.environ.get(q_cfg["api_key_env"], "")
    if not (url and api_key):
        raise RuntimeError(
            f"Qdrant client needs {q_cfg['url_env']} and {q_cfg['api_key_env']} "
            "in the environment"
        )
    return AsyncQdrantClient(
        url=url,
        api_key=api_key,
        prefer_grpc=bool(q_cfg.get("prefer_grpc", False)),
        timeout=60,
    )
