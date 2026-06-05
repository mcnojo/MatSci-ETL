"""I/O models for the batch workflows.

BatchRunWorkflow:    BatchRunInput  -> BatchRunOutput
ShardWorkflow:       ShardInput     -> ShardOutput
Both produce/consume ItemResult, which is the per-PDF outcome carried up
from a ProcessPdfWorkflow child.
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

from ..models import BatchItem


class ItemResult(BaseModel):
    """One PDF's outcome inside a shard. Successes carry tree pointers and
    metrics; failures carry a string error tail (workflow errors do not
    serialize their full Python traceback)."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    pdf_uri: str
    status: Literal["success", "failure"]
    workflow_id: str
    tree_path: Optional[str] = None
    node_count: Optional[int] = None
    total_pages: Optional[int] = None
    metrics_summary: Optional[dict] = None
    error: Optional[str] = None


class ShardInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    shard_index: int
    items: list[BatchItem]
    pipeline_config: dict
    max_in_flight: int = 8           # bounded concurrency over PDF children


class ShardOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    shard_index: int
    items: list[ItemResult]


class BatchRunInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    manifest_uri: str                # s3:// or local path read by fetch_manifest_activity
    pipeline_config: dict            # already-resolved pipeline config (URLs etc.)
    report_root: str                 # e.g. "s3://chem-lit-artifacts/batches"
    shard_size: int = 50
    shards_in_flight: int = 8        # bounded concurrency over ShardWorkflow children
    pdfs_per_shard_in_flight: int = 8


class BatchRunOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    total_items: int
    success_count: int
    failure_count: int
    report_uris: dict[str, str]      # summary_uri, per_item_uri, failures_uri
