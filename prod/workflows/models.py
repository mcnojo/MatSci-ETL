"""Input/output models for the ProcessPdf workflow."""

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
    tree_path: str      # output location (file:// or s3://)
    node_count: int
    total_pages: int
