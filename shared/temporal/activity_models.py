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


# Indexing route (BM25 + hybrid RAG). URI-not-payload: chunks + embeddings
# stage through S3; only URIs cross Temporal history.

class LoadTreeInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    tree_uri: str


class LoadTreeOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    paper_id: str
    pdf_path: str
    total_pages: int
    tree_json_uri: str        # echoes input; downstream reads via s3_io


class ChunkDocumentInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    document_id: str
    run_id: str
    tree_uri: str
    pages_uri: str
    config: dict              # carries retrieval.chunking.max_tokens etc.


class ChunkDocumentOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    chunks_uri: str
    chunk_count: int
    total_tokens: int


class EmbedChunksInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    chunks_uri: str
    embeddings_uri_out: str    # deterministic destination — activity writes here
    config: dict


class EmbedChunksOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    embeddings_uri: str
    embedded_count: int
    dimension: int
    started_at: float
    ended_at: float


class IndexChunksInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    paper_id: str             # wipe_paper target — pre-write cleanup of prior chunks
    chunks_uri: str
    embeddings_uri: str
    index_name: str
    config: dict


class IndexChunksOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    indexed_count: int
    index_name: str
