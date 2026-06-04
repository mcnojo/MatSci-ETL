"""Input/output models for the ProcessPdf workflow and its activities."""

from pydantic import BaseModel, ConfigDict


# Workflow I/O

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


# Per-call activity I/O

class LlmTextCallInput(BaseModel):
    """One text-LLM call. The activity dispatches via etl.pipeline.llm_calls."""
    model_config = ConfigDict(frozen=True)

    config: dict                # pipeline config (needed to init the provider client)
    model: str                  # explicit model — workflow chooses strong vs. fast
    prompt: str
    json_mode: bool = False
    temperature: float = 0.0


class LlmTextCallOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str                  # resolved model name (may differ from input.model)
    content: str
    finish_reason: str
    started_at: float           # Unix wall-clock at LLM call start (time.time())
    ended_at: float             # Unix wall-clock at LLM call end (time.time())
    input_tokens: int
    output_tokens: int


class ChandraCallInput(BaseModel):
    """One Chandra OCR call for a single asset image."""
    model_config = ConfigDict(frozen=True)

    base_url: str               # resolved vLLM endpoint
    api_key: str
    model: str                  # ocr model id
    image_path: str             # local filesystem path; activity reads + b64-encodes
    prompt: str                 # CHANDRA_OCR_LAYOUT_PROMPT or variant
    max_tokens: int = 4096


class ChandraCallOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    started_at: float           # Unix wall-clock at OCR call start (time.time())
    ended_at: float             # Unix wall-clock at OCR call end (time.time())
    input_tokens: int
    output_tokens: int
