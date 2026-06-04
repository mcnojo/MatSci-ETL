"""Input/output models for ProcessPdfWorkflow (live).

Per-call activity I/O models (LlmTextCallInput/Output, ChandraCallInput/Output)
live in prod/shared_infra/activity_models.py because they are used by both the
live ProcessPdfWorkflow and the batch path's per-PDF fan-out.
"""

from pydantic import BaseModel, ConfigDict


class ProcessPdfWorkflowInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: str
    run_id: str
    pdf_path: str               # local path or s3:// URI to the source PDF
    config: dict                # full pipeline config (serializable YAML dict)
    skip_enrichment: bool = False


class ProcessPdfWorkflowOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: str
    run_id: str
    tree_path: str              # output location (file:// or s3://)
    node_count: int
    total_pages: int
    metrics_summary: dict       # aggregated per-call timings and token usage
