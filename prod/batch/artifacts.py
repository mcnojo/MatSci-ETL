"""Batch-specific artifact IO: manifest read + flat per-item/failures/summary writer.

Path layout (report_root is the bucket root; each motif's writer prepends its own prefix):
    s3://<bucket>/batches/<batch_id>/manifest.json
    s3://<bucket>/batches/<batch_id>/report/{summary.json,per_item.csv,failures.jsonl,report.json,report.md}
"""

import csv
import io
import json

from shared.s3_io import get_bytes, put_bytes

from .models import BatchManifest


def read_manifest(manifest_uri: str) -> BatchManifest:
    """Read+validate a manifest from `s3://...` or a local path."""
    return BatchManifest.model_validate_json(get_bytes(manifest_uri).decode("utf-8"))


def report_prefix(report_root: str, batch_id: str) -> str:
    return f"{report_root.rstrip('/')}/batches/{batch_id}/report"


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

    put_bytes(summary_uri, summary_body, "application/json")
    put_bytes(per_item_uri, per_item_body, "text/csv")
    put_bytes(failures_uri, failures_body, "application/x-ndjson")

    return {
        "summary_uri": summary_uri,
        "per_item_uri": per_item_uri,
        "failures_uri": failures_uri,
    }
