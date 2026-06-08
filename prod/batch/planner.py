"""Manifest sharding: pure slicing keeping each shard's event history under
Temporal's 50MB/~50k-event soft limit per workflow.
"""

from .models import BatchItem, BatchManifest


DEFAULT_SHARD_SIZE = 50


def shard_manifest(
    manifest: BatchManifest,
    shard_size: int = DEFAULT_SHARD_SIZE,
) -> list[list[BatchItem]]:
    """Contiguous slices in manifest order so shard-N is items [N×size, (N+1)×size)."""
    if shard_size <= 0:
        raise ValueError(f"shard_size must be positive, got {shard_size}")
    items = manifest.items
    return [items[i : i + shard_size] for i in range(0, len(items), shard_size)]


def batch_workflow_id(batch_id: str) -> str:
    return f"batch-{batch_id}"


def shard_workflow_id(batch_id: str, shard_index: int) -> str:
    return f"batch-{batch_id}-shard-{shard_index:04d}"


def per_pdf_workflow_id(batch_id: str, document_id: str) -> str:
    return f"batch-{batch_id}-pdf-{document_id}"
