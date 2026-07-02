"""Chunk — one retrievable unit for BM25 + dense indexing.

Emitted by pipeline.chunker from a completed DocumentTree. Each chunk carries
enough tree provenance that retrieval can filter by section, bias by depth,
and trace a hit back to the exact node it came from. `doc_id` is fully
deterministic across re-runs so bulk-index writes are idempotent, and the
paired `chunker_version` stamps disambiguate an index that spans a chunker
strategy change.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


CHUNKER_VERSION = "v1"

# source_kind values — where the text physically came from. Retrieval / QA can
# filter or reweight by provenance (e.g. drop LLM-summarized text for strict
# extractive tasks).
SOURCE_PYMUPDF = "pymupdf"            # raw born-digital text via PyMuPDF.get_text()
SOURCE_CHANDRA_MD = "chandra_markdown"  # HTML tables from chandra converted to markdown
SOURCE_LAYOUT_CAPTION = "layout_caption"  # caption text lifted from PDF near a detected element
SOURCE_LLM_ABSTRACT = "llm_abstract"  # extractive abstract produced by tree_llm

# kind values — the semantic shape of the chunk. Independent of source_kind:
# a "table" chunk is chandra-markdown; a "section_text" chunk is pymupdf.
KIND_ABSTRACT = "abstract"
KIND_SECTION_TEXT = "section_text"
KIND_TABLE = "table"
KIND_CAPTION = "caption"


class Chunk(BaseModel):
    """One retrievable unit. Immutable after emission by the chunker.

    `embedding` is populated by the embed activity; None until then. Keep the
    same object through both stages to avoid a second schema for pre-embedded
    chunks.
    """
    model_config = ConfigDict(frozen=True)

    doc_id: str                    # "{paper_id}:{node_id}:{sub_index}"
    paper_id: str
    node_id: str
    sub_index: int                 # 0 for one-shot leaves; >0 for split leaves
    node_title: str                # local leaf title (or "Abstract" / element caption)
    breadcrumb: list[str] = Field(default_factory=list)  # root -> leaf titles
    depth: int = 0                 # len(breadcrumb) — retrieval-side reweight signal
    position: int = 0              # emission ordinal within the paper (DFS)
    page_start: int                # 1-based, inclusive
    page_end: int                  # 1-based, inclusive
    kind: str                      # KIND_* — semantic shape of the chunk
    source_kind: str               # SOURCE_* — origin of the raw text
    text: str
    token_count: int
    tree_uri: Optional[str] = None       # source tree.json — traceback pointer
    chunker_version: str = CHUNKER_VERSION
    embedding: Optional[list[float]] = None
