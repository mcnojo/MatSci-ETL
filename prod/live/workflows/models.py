"""Input/output models for ProcessPdfWorkflow (live). Per-call activity I/O
models live in shared/temporal/activity_models.py — shared with batch."""

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
    # Populated when retrieval.index_enabled and the tail IndexDocumentWorkflow
    # child succeeded. None means indexing was skipped (toggle off).
    index_summary: dict | None = None


class IndexDocumentWorkflowInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: str
    run_id: str
    tree_uri: str               # local path or s3:// URI to finalized tree.json
    config: dict                # full pipeline config (must include retrieval.* + embedding_server.*)
    index_name: str | None = None  # overrides retrieval.opensearch.index_name


class IndexDocumentWorkflowOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: str
    run_id: str
    index_name: str
    chunk_count: int
    embedded_count: int
    indexed_count: int
    total_tokens: int
