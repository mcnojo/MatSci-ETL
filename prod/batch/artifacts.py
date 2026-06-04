"""S3 artifact IO for the batch path — manifest read, report write.

Kept narrow: this module owns the S3 path layout for batch-scoped artifacts
and the (de)serialization of manifests + reports. Workflows treat these
helpers as opaque IO calls.

Path layout:
    s3://<bucket>/batches/<batch_id>/manifest.json
    s3://<bucket>/batches/<batch_id>/report/summary.json
    s3://<bucket>/batches/<batch_id>/report/per_item.csv
    s3://<bucket>/batches/<batch_id>/report/failures.jsonl
"""

import csv
import io
import json
from pathlib import Path
from urllib.parse import urlparse

import boto3

from .models import BatchManifest


def _split_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"expected s3:// URI, got {uri}")
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not bucket or not key:
        raise ValueError(f"malformed s3 URI: {uri}")
    return bucket, key


def read_manifest(manifest_uri: str) -> BatchManifest:
    """Read and validate a manifest from S3 or local disk.

    `manifest_uri` may be either an `s3://bucket/key.json` URI or a local
    filesystem path. Validation is via the Pydantic model.
    """
    if manifest_uri.startswith("s3://"):
        bucket, key = _split_s3_uri(manifest_uri)
        s3 = boto3.client("s3")
        obj = s3.get_object(Bucket=bucket, Key=key)
        raw = obj["Body"].read().decode("utf-8")
    else:
        raw = Path(manifest_uri).read_text(encoding="utf-8")
    return BatchManifest.model_validate_json(raw)


def report_prefix(report_root: str, batch_id: str) -> str:
    """Compose the S3 prefix where this batch's report artifacts live.

    `report_root` is configured operator-side (e.g. "s3://chem-lit-artifacts/batches").
    """
    return f"{report_root.rstrip('/')}/{batch_id}/report"


def write_report_files(
    report_root: str,
    batch_id: str,
    *,
    summary: dict,
    per_item: list[dict],
    failures: list[dict],
) -> dict[str, str]:
    """Write summary.json, per_item.csv, failures.jsonl. Returns the S3 URIs."""
    prefix = report_prefix(report_root, batch_id)
    summary_uri = f"{prefix}/summary.json"
    per_item_uri = f"{prefix}/per_item.csv"
    failures_uri = f"{prefix}/failures.jsonl"

    summary_body = json.dumps(summary, indent=2, ensure_ascii=False).encode("utf-8")
    failures_body = "\n".join(json.dumps(f, ensure_ascii=False) for f in failures).encode("utf-8")

    per_item_buf = io.StringIO()
    if per_item:
        writer = csv.DictWriter(per_item_buf, fieldnames=list(per_item[0].keys()))
        writer.writeheader()
        writer.writerows(per_item)
    per_item_body = per_item_buf.getvalue().encode("utf-8")

    _put(summary_uri, summary_body, "application/json")
    _put(per_item_uri, per_item_body, "text/csv")
    _put(failures_uri, failures_body, "application/x-ndjson")

    return {
        "summary_uri": summary_uri,
        "per_item_uri": per_item_uri,
        "failures_uri": failures_uri,
    }


def _put(uri: str, body: bytes, content_type: str) -> None:
    bucket, key = _split_s3_uri(uri)
    boto3.client("s3").put_object(
        Bucket=bucket, Key=key, Body=body, ContentType=content_type,
    )
