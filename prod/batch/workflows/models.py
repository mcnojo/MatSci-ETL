"""I/O models for the batch workflows.

BatchRunWorkflow:    BatchRunInput  -> BatchRunOutput
ShardWorkflow:       ShardInput     -> ShardOutput
Both produce/consume ItemResult, which is the per-PDF outcome carried up
from a ProcessPdfWorkflow child.
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, model_validator

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
    report_root: str                 # bucket root, e.g. "s3://chem-lit-artifacts"; writers prepend /batches/ etc.
    shard_size: int = 50
    shards_in_flight: int = 8        # bounded concurrency over ShardWorkflow children
    pdfs_per_shard_in_flight: int = 8

    # Fleet lifecycle. Populated together (all-or-none); when omitted, the
    # workflow assumes the caller is managing the fleet out-of-band (local
    # dev, `cli submit --no-manage-fleet`) and skips scale-up, poller wait,
    # and scale-down. Values are sourced from prod/batch terraform outputs at
    # `cli submit` time.
    region: Optional[str] = None
    cpu_queue_asg_name: Optional[str] = None
    gpu_queue_asg_name: Optional[str] = None
    cpu_queue_desired: Optional[int] = None
    gpu_queue_desired: Optional[int] = None
    worker_registration_timeout_s: int = 600

    @model_validator(mode="after")
    def _fleet_all_or_none(self) -> "BatchRunInput":
        fleet_fields = (
            self.region, self.cpu_queue_asg_name, self.gpu_queue_asg_name,
            self.cpu_queue_desired, self.gpu_queue_desired,
        )
        n_set = sum(1 for f in fleet_fields if f is not None)
        if 0 < n_set < len(fleet_fields):
            raise ValueError(
                "Fleet fields are all-or-none. Set every one of {region, "
                "cpu_queue_asg_name, gpu_queue_asg_name, cpu_queue_desired, "
                "gpu_queue_desired} to manage the fleet, or leave all None."
            )
        return self

    @property
    def manages_fleet(self) -> bool:
        return self.region is not None


class BatchRunOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    total_items: int
    success_count: int
    failure_count: int
    report_uris: dict[str, str]      # summary_uri, per_item_uri, failures_uri
    # Populated when retrieval.index_enabled and the auto-index tail ran.
    # None means indexing was skipped (toggle off) or no items succeeded OCR.
    index_summary: Optional[dict] = None


# Indexing route (parallel motif). Distinct workflows so per-item child typing
# stays clean — Temporal doesn't polymorphize child workflow signatures.

class IndexBatchItem(BaseModel):
    """One document in an indexing manifest. Distinct from BatchItem: no PDF
    URI, just the tree pointer produced by a prior ProcessPdfWorkflow run."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    tree_uri: str


class IndexItemResult(BaseModel):
    """One document's outcome inside an index shard."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    tree_uri: str
    status: Literal["success", "failure"]
    workflow_id: str
    collection_name: Optional[str] = None
    chunk_count: Optional[int] = None
    embedded_count: Optional[int] = None
    indexed_count: Optional[int] = None
    total_tokens: Optional[int] = None
    error: Optional[str] = None


class ShardIndexInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    shard_index: int
    items: list[IndexBatchItem]
    pipeline_config: dict            # must contain retrieval.* + embedding_server.*
    max_in_flight: int = 8


class ShardIndexOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    shard_index: int
    items: list[IndexItemResult]


class BatchIndexRunInput(BaseModel):
    """Top-level parent input for an indexing batch. Peer of BatchRunInput —
    owns fleet lifecycle (scale up + await pollers + scale down) around the
    ShardIndexWorkflow fan-out. When the fleet fields are all None (local
    dev, or `--no-manage-fleet`), the workflow assumes an out-of-band fleet
    and skips those stages.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    manifest_uri: str
    pipeline_config: dict
    report_root: str
    shard_size: int = 50
    shards_in_flight: int = 8
    documents_per_shard_in_flight: int = 8

    # Fleet lifecycle — same all-or-none contract as BatchRunInput.
    region: Optional[str] = None
    cpu_queue_asg_name: Optional[str] = None
    gpu_queue_asg_name: Optional[str] = None
    cpu_queue_desired: Optional[int] = None
    gpu_queue_desired: Optional[int] = None
    worker_registration_timeout_s: int = 600

    @model_validator(mode="after")
    def _fleet_all_or_none(self) -> "BatchIndexRunInput":
        fleet_fields = (
            self.region, self.cpu_queue_asg_name, self.gpu_queue_asg_name,
            self.cpu_queue_desired, self.gpu_queue_desired,
        )
        n_set = sum(1 for f in fleet_fields if f is not None)
        if 0 < n_set < len(fleet_fields):
            raise ValueError(
                "Fleet fields are all-or-none. Set every one of {region, "
                "cpu_queue_asg_name, gpu_queue_asg_name, cpu_queue_desired, "
                "gpu_queue_desired} to manage the fleet, or leave all None."
            )
        return self

    @property
    def manages_fleet(self) -> bool:
        return self.region is not None


class BatchIndexRunOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    total_items: int
    success_count: int
    failure_count: int
    report_uris: dict[str, str]      # summary_uri, per_item_uri, failures_uri
