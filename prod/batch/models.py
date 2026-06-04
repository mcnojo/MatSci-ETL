"""Pydantic models for the batch manifest and workflow I/O.

The manifest is the operator's input contract. It is stored as JSON, either on
local disk or in S3, and read by the BatchRunWorkflow at run start. Per-item
processing is delegated to ProcessPdfWorkflow (live), keyed by document_id.

Workflow-level I/O models live alongside their workflow under
prod/batch/workflows/.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator


class BatchItem(BaseModel):
    """One PDF in a batch manifest."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    pdf_uri: str

    @field_validator("document_id")
    @classmethod
    def _document_id_format(cls, v: str) -> str:
        if not v or not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError(
                "document_id must be non-empty and contain only alphanumerics, "
                "hyphens, or underscores"
            )
        return v


class BatchManifest(BaseModel):
    """A batch of PDFs submitted as a single job.

    `batch_id` is the workflow ID for BatchRunWorkflow and the S3 prefix for
    all batch-scoped artifacts. Operator-supplied so re-runs of the same
    corpus are reproducible.

    `config_overrides` is layered on top of the pipeline config at workflow
    start (e.g. to use a faster tree_llm model for cheap validation runs).
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    items: list[BatchItem]
    config_overrides: Optional[dict] = None

    @field_validator("batch_id")
    @classmethod
    def _batch_id_format(cls, v: str) -> str:
        if not v or not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError(
                "batch_id must be non-empty and contain only alphanumerics, "
                "hyphens, or underscores"
            )
        return v

    @field_validator("items")
    @classmethod
    def _items_nonempty_and_unique(cls, v: list[BatchItem]) -> list[BatchItem]:
        if not v:
            raise ValueError("manifest must contain at least one item")
        seen = set()
        for item in v:
            if item.document_id in seen:
                raise ValueError(f"duplicate document_id in manifest: {item.document_id}")
            seen.add(item.document_id)
        return v
