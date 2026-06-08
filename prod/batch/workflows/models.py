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
    report_root: str                 # e.g. "s3://chem-lit-artifacts/batches"
    shard_size: int = 50
    shards_in_flight: int = 8        # bounded concurrency over ShardWorkflow children
    pdfs_per_shard_in_flight: int = 8

    # Fleet lifecycle. Populated together (all-or-none); when omitted, the
    # workflow assumes the caller is managing the fleet out-of-band (local
    # dev, `cli submit` against a pre-running fleet) and skips scale-up,
    # poller wait, and scale-down. Values flow from batch_config.yaml's
    # `fleet` block through the CLI/Lambda into the workflow input.
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
