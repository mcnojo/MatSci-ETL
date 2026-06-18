"""Per-call activity I/O models shared between live and batch motifs."""

from pydantic import BaseModel, ConfigDict


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
    image_uri: str              # s3:// URI or local path; activity reads via shared.s3_io
    prompt: str                 # CHANDRA_OCR_LAYOUT_PROMPT or variant
    max_tokens: int = 4096


class ChandraCallOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    started_at: float           # Unix wall-clock at OCR call start (time.time())
    ended_at: float             # Unix wall-clock at OCR call end (time.time())
    input_tokens: int
    output_tokens: int
