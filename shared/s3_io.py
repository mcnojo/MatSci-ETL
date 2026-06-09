"""S3 IO primitives shared across motifs. Local paths supported for dev.

put_bytes / get_bytes accept either `s3://bucket/key` or a filesystem path —
report writers and artifact stores route uniformly through here.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import boto3


def split_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"expected s3:// URI, got {uri}")
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not bucket or not key:
        raise ValueError(f"malformed s3 URI: {uri}")
    return bucket, key


def put_bytes(uri: str, body: bytes, content_type: str) -> None:
    if uri.startswith("s3://"):
        bucket, key = split_s3_uri(uri)
        boto3.client("s3").put_object(
            Bucket=bucket, Key=key, Body=body, ContentType=content_type,
        )
        return
    path = Path(uri)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def get_bytes(uri: str) -> bytes:
    if uri.startswith("s3://"):
        bucket, key = split_s3_uri(uri)
        obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        return obj["Body"].read()
    return Path(uri).read_bytes()
