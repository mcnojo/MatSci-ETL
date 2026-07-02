"""OpenSearch client factory.

Resolves the endpoint URL and picks the right auth strategy:

- Self-hosted (`endpoint: https://…`): basic auth from env vars declared in the
  config (`user_env` / `password_env`). Verify-certs is disabled for the common
  single-node self-signed setup — surface that explicitly in the config.

- Amazon OpenSearch Service (`endpoint: https://….es.amazonaws.com`): IAM
  SigV4 via requests-aws4auth. Boto's default credential chain resolves the
  role. `region` in the config is required for SigV4.
"""

from __future__ import annotations

import os
from typing import Any


def resolve_endpoint(config: dict) -> str:
    """`retrieval.opensearch.endpoint` — no legacy fallback."""
    try:
        return config["retrieval"]["opensearch"]["endpoint"]
    except KeyError as e:
        raise KeyError(f"Missing retrieval.opensearch.endpoint in config ({e})")


def build_client(config: dict) -> Any:
    """Return an initialized opensearchpy.OpenSearch pointed at the config endpoint.

    Auth mode is driven by `retrieval.opensearch.auth`:
      - "basic": use user_env/password_env
      - "aws_sigv4": use boto3 default chain + region for AOS
    """
    from opensearchpy import OpenSearch, RequestsHttpConnection  # deferred: heavy import

    os_cfg = config["retrieval"]["opensearch"]
    endpoint = resolve_endpoint(config)
    verify_certs = bool(os_cfg.get("verify_certs", True))
    auth_mode = os_cfg.get("auth", "basic")

    common = dict(
        hosts=[endpoint],
        use_ssl=endpoint.startswith("https://"),
        verify_certs=verify_certs,
        ssl_show_warn=verify_certs,
        connection_class=RequestsHttpConnection,
        timeout=30,
        max_retries=3,
        retry_on_timeout=True,
    )

    if auth_mode == "basic":
        user = os.environ.get(os_cfg.get("user_env", "OPENSEARCH_USER"), "")
        password = os.environ.get(os_cfg.get("password_env", "OPENSEARCH_PASSWORD"), "")
        if not (user and password):
            raise RuntimeError(
                "OpenSearch basic auth requires user_env and password_env to be set"
            )
        return OpenSearch(http_auth=(user, password), **common)

    if auth_mode == "aws_sigv4":
        import boto3
        from requests_aws4auth import AWS4Auth

        region = os_cfg.get("region")
        if not region:
            raise RuntimeError("aws_sigv4 auth requires retrieval.opensearch.region")
        creds = boto3.Session().get_credentials()
        if creds is None:
            raise RuntimeError("aws_sigv4 auth: no AWS credentials resolved")
        awsauth = AWS4Auth(
            creds.access_key, creds.secret_key, region, "es",
            session_token=creds.token,
        )
        return OpenSearch(http_auth=awsauth, **common)

    raise ValueError(f"retrieval.opensearch.auth must be basic|aws_sigv4, got {auth_mode!r}")
