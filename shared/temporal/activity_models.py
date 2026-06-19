"""Per-call activity I/O models shared between live and batch motifs."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class PageRangeSpec(BaseModel):
    """Page-text kwarg — resolved against pages_uri by the activity."""
    model_config = ConfigDict(frozen=True)
    indices: list[int]
    wrap: str = "raw"                # "raw" | "physical_index"
    transform_dots: bool = False
    overlap_pre: list[int] = []
    overlap_post: list[int] = []
    extract_section_only: bool = False


class PromptSpec(BaseModel):
    """Renderer name/style + kwargs. page_kwargs resolve against pages_uri."""
    model_config = ConfigDict(frozen=True)
    name: str
    style: str
    small_kwargs: dict[str, Any] = {}
    page_kwargs: dict[str, PageRangeSpec] = {}
    pages_uri: str | None = None


class LlmTextCallInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    prompt_spec: PromptSpec
    config_uri: str
    model: str
    json_mode: bool = False
    temperature: float = 0.0


class LlmTextCallOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    content: str
    finish_reason: str
    started_at: float
    ended_at: float
    input_tokens: int
    output_tokens: int


class ChandraCallInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_url: str
    api_key: str
    model: str
    image_uri: str
    prompt: str
    max_tokens: int = 4096


class ChandraCallOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    started_at: float
    ended_at: float
    input_tokens: int
    output_tokens: int
