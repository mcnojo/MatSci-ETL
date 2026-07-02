"""Manifest sharding: pure slicing keeping each shard's event history under
Temporal's 50MB/~50k-event soft limit per workflow.

Also owns the layout convention for batch manifests in S3 — single source of
truth shared by the uploader (bin/batch/submit.sh) and the workflow starter
(prod/batch/cli.py).
"""

from .models import BatchItem, BatchManifest, IndexBatchManifest, IndexManifestItem


DEFAULT_SHARD_SIZE = 50

# Manifest layout: s3://<artifact_bucket>/<INCOMING_PREFIX><batch_id>/manifest.json
# PDFs:           s3://<artifact_bucket>/<INCOMING_PREFIX><batch_id>/pdfs/<doc_id>.pdf
INCOMING_PREFIX = "batches/incoming/"
# Indexing manifests live under a parallel prefix so `idx-<x>` and `batch-<x>`
# never share an S3 namespace or a workflow id.
INCOMING_INDEX_PREFIX = "batches/incoming-index/"


def manifest_uri(artifact_bucket: str, batch_id: str) -> str:
    return f"s3://{artifact_bucket}/{INCOMING_PREFIX}{batch_id}/manifest.json"


def index_manifest_uri(artifact_bucket: str, batch_id: str) -> str:
    return f"s3://{artifact_bucket}/{INCOMING_INDEX_PREFIX}{batch_id}/manifest.json"


def shard_manifest(
    manifest: BatchManifest,
    shard_size: int = DEFAULT_SHARD_SIZE,
) -> list[list[BatchItem]]:
    """Contiguous slices in manifest order so shard-N is items [N×size, (N+1)×size)."""
    if shard_size <= 0:
        raise ValueError(f"shard_size must be positive, got {shard_size}")
    items = manifest.items
    return [items[i : i + shard_size] for i in range(0, len(items), shard_size)]


def shard_index_manifest(
    manifest: IndexBatchManifest,
    shard_size: int = DEFAULT_SHARD_SIZE,
) -> list[list[IndexManifestItem]]:
    """Same slicing shape as `shard_manifest`, for indexing manifests."""
    if shard_size <= 0:
        raise ValueError(f"shard_size must be positive, got {shard_size}")
    items = manifest.items
    return [items[i : i + shard_size] for i in range(0, len(items), shard_size)]


def batch_workflow_id(batch_id: str) -> str:
    return f"batch-{batch_id}"


def batch_index_workflow_id(batch_id: str) -> str:
    return f"idx-batch-{batch_id}"


def shard_workflow_id(batch_id: str, shard_index: int) -> str:
    return f"batch-{batch_id}-shard-{shard_index:04d}"


def per_pdf_workflow_id(batch_id: str, document_id: str) -> str:
    return f"batch-{batch_id}-pdf-{document_id}"


def index_shard_workflow_id(batch_id: str, shard_index: int) -> str:
    return f"idx-{batch_id}-shard-{shard_index:04d}"


def per_index_workflow_id(batch_id: str, document_id: str) -> str:
    return f"idx-{batch_id}-{document_id}"
