"""Manifest sharding logic — pure functions, no side effects.

The planner takes a `BatchManifest` and returns a list of shards. Each shard
is a contiguous slice of items that a ShardWorkflow will process. Sharding
keeps any single workflow's event history bounded (Temporal's 50 MB / ~50k
events soft limit per workflow).
"""

from .models import BatchItem, BatchManifest


DEFAULT_SHARD_SIZE = 50


def shard_manifest(
    manifest: BatchManifest,
    shard_size: int = DEFAULT_SHARD_SIZE,
) -> list[list[BatchItem]]:
    """Split manifest items into shards of at most `shard_size`.

    Shards are contiguous slices in manifest order so that:
      - the report can reference shards as ranges (e.g. "shard 3: items 100-149"), and
      - operator intuition matches workflow IDs (shard-0 is the first 50 items).
    """
    if shard_size <= 0:
        raise ValueError(f"shard_size must be positive, got {shard_size}")
    items = manifest.items
    return [items[i : i + shard_size] for i in range(0, len(items), shard_size)]


def shard_workflow_id(batch_id: str, shard_index: int) -> str:
    """Deterministic workflow ID for a shard within a batch."""
    return f"batch-{batch_id}-shard-{shard_index:04d}"


def per_pdf_workflow_id(batch_id: str, document_id: str) -> str:
    """Deterministic workflow ID for the per-PDF ProcessPdfWorkflow spawn.

    Keyed on (batch_id, document_id) so the same document re-run in a new
    batch gets a fresh workflow but the same logical identity. document_id
    alone would collide across batches; including batch_id keeps runs
    distinguishable in the Temporal UI without losing the document anchor.
    """
    return f"batch-{batch_id}-pdf-{document_id}"
