"""CPU-lane Temporal activities + their I/O models.

The five stage-level activities — load_pages, extract_assets, attach_ocr,
assign_elements, finalize — plus the Pydantic I/O models they share with the
workflow. Pulls the heavy CV/ML stack (torch, doclayout-yolo, opencv)
transitively via AssetExtractor; lives behind the pipeline-cpu extra and is
only loaded by workers that register the CPU-lane queues or the workflows
that fan out to those activities.

URI-not-payload: the `page_elements` dict (large for asset-rich PDFs) is
written to S3 by extract_assets and read back by attach_ocr / assign_elements
through `shared.s3_io`. Only the URI flows through Temporal history, so the
4 MB gRPC ceiling stops being a function of PDF size.
"""

import asyncio
import io
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pymupdf
from pydantic import BaseModel, ConfigDict
from temporalio import activity

from shared.s3_io import get_bytes, put_bytes
from shared.schemas import Chunk, DocumentTree
from shared.temporal.activity_models import (
    ChunkDocumentInput,
    ChunkDocumentOutput,
    IndexChunksInput,
    IndexChunksOutput,
    LoadTreeInput,
    LoadTreeOutput,
)

from .asset_extractor import AssetExtractor
from .chunker import chunk_document
from .enricher import assign_elements_to_tree, attach_raw_text_to_tree
from .heartbeat import await_with_heartbeats
from .tree_logic import count_tokens


# I/O models

class LoadPagesInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    pdf_path: str
    document_id: str
    run_id: str
    config: dict


class LoadPagesOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    # Page text stays out of history; downstream activities read pages_uri.
    token_counts: list[int]
    total_pages: int
    is_likely_scanned: bool
    pages_uri: str
    config_uri: str


class ExtractAssetsInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    pdf_path: str
    document_id: str
    run_id: str
    config: dict
    page_ranges: list[tuple[int, int]]


# One asset-eligible element. Returned alongside page_elements_uri so the
# workflow can drive OCR fan-out without re-reading the (potentially large)
# page_elements dict from S3. Stays small: ~200 B per element × N elements.
class OcrTarget(BaseModel):
    model_config = ConfigDict(frozen=True)
    page_idx: int
    elem_idx: int
    element_id: str
    asset_uri: str


class ExtractAssetsOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    # URI of the JSON-dumped dict[page_index, list[element_dict]] in S3 (or
    # local kb_root in dev). attach_ocr / assign_elements read this back via
    # shared.s3_io — workflow history only carries the URI.
    page_elements_uri: str
    # page_index -> URI of the rendered page PNG. Empty when save_page_images=false.
    # Small (URIs only) so it stays inline.
    page_image_uris: dict[int, str]
    # Drives the workflow's OCR fan-out without forcing it to read the dict.
    ocr_targets: list[OcrTarget]
    total_elements: int
    element_counts: dict[str, int]


# One per-element OCR mutation produced by the workflow after chandra fan-out.
# Identified by element_id (authoritative) + page_idx/elem_idx (fast-path index).
class OcrUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)
    element_id: str
    page_idx: int
    elem_idx: int
    ocr_text: str | None = None
    ocr_parsed: dict | None = None


class AttachOcrInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    page_elements_uri: str
    ocr_updates: list[OcrUpdate]
    document_id: str
    config: dict


class AttachOcrOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    page_elements_uri: str   # new URI pointing at the OCR-enriched dict


class AssignElementsInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    tree: DocumentTree
    page_elements_uri: str
    page_image_uris: dict[int, str]
    pdf_path: str
    document_id: str
    config: dict


class AssignElementsOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    tree: DocumentTree


class AttachRawTextInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    tree: DocumentTree
    pages_uri: str


class AttachRawTextOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    tree: DocumentTree


class FinalizeInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    tree: DocumentTree
    output_path: str
    pretty_print: bool = True


class FinalizeOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    tree_path: str


# Helpers

@contextmanager
def _localized_pdf(uri_or_path: str):
    """Yield a local filesystem path for the PDF.

    Activities receive s3:// URIs from the manifest, but PyMuPDF and
    AssetExtractor open files by local path. Downloads to a NamedTemporaryFile
    on the worker, cleaned up on exit. Local paths pass through unchanged.
    """
    if uri_or_path.startswith("s3://"):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(get_bytes(uri_or_path))
            tmp.flush()
            yield tmp.name
    else:
        yield uri_or_path


def _get_page_tokens(pdf_path: str) -> list[tuple[str, int]]:
    doc = pymupdf.open(pdf_path)
    page_list = []
    for page in doc:
        text = page.get_text()
        page_list.append((text, count_tokens(text)))
    doc.close()
    return page_list


def _is_likely_scanned(page_list: list[tuple[str, int]], threshold: int = 30) -> bool:
    word_counts = [len(text.split()) for text, _ in page_list]
    if not word_counts:
        return True
    median_words = sorted(word_counts)[len(word_counts) // 2]
    return median_words < threshold


_PATH_FIELDS: frozenset[str] = frozenset({"pdf_path", "asset_uri"})
_PATH_LIST_FIELDS: frozenset[str] = frozenset({"page_image_uris"})


def _portablize_paths(obj, anchor: Path) -> None:
    """In-place: rewrite known path fields to be relative to `anchor`.

    Only touches absolute values in fields known to hold filesystem paths
    (pdf_path, asset_uri, page_image_uris). s3:// URIs and other non-local
    strings pass through untouched (Path("s3://…").is_absolute() is False).
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _PATH_FIELDS and isinstance(v, str) and Path(v).is_absolute():
                obj[k] = os.path.relpath(v, anchor)
            elif k in _PATH_LIST_FIELDS and isinstance(v, list):
                obj[k] = [
                    os.path.relpath(p, anchor)
                    if isinstance(p, str) and Path(p).is_absolute() else p
                    for p in v
                ]
            else:
                _portablize_paths(v, anchor)
    elif isinstance(obj, list):
        for item in obj:
            _portablize_paths(item, anchor)


# Activities

def _run_artifact_uri(config: dict, document_id: str, run_id: str, leaf: str) -> str:
    # {run_id} isolates re-runs of the same document.
    prefix = config["output"]["assets_uri_prefix"].rstrip("/")
    return f"{prefix}/{document_id}/runs/{run_id}/{leaf}"


@activity.defn(name="process-pdf_load-pages")
async def load_pages_activity(input: LoadPagesInput) -> LoadPagesOutput:
    with _localized_pdf(input.pdf_path) as local_pdf:
        page_list = _get_page_tokens(local_pdf)

    pages_uri = _run_artifact_uri(input.config, input.document_id, input.run_id, "pages.json")
    put_bytes(pages_uri, json.dumps(page_list).encode("utf-8"), "application/json")

    config_uri = _run_artifact_uri(input.config, input.document_id, input.run_id, "config.json")
    put_bytes(config_uri, json.dumps(input.config).encode("utf-8"), "application/json")

    return LoadPagesOutput(
        token_counts=[tokens for _, tokens in page_list],
        total_pages=len(page_list),
        is_likely_scanned=_is_likely_scanned(page_list),
        pages_uri=pages_uri,
        config_uri=config_uri,
    )


def _page_elements_uri(config: dict, document_id: str, suffix: str) -> str:
    """Deterministic URI for the page_elements dump, keyed on document_id.

    Two suffixes in play:
      - "raw"      — emitted by extract_assets (pre-OCR)
      - "enriched" — emitted by attach_ocr (post-OCR mutations applied)

    Re-runs of the same workflow overwrite the same key, so retries are
    idempotent — same as build_tree's tree_uri convention.
    """
    prefix = config["output"]["assets_uri_prefix"].rstrip("/")
    return f"{prefix}/{document_id}/page_elements_{suffix}.json"


def _dump_page_elements(uri: str, page_elements: dict[int, list[dict]]) -> None:
    """JSON-encode + publish via shared.s3_io. Int page keys become strings on
    the wire (JSON limitation); _load_page_elements reverses on read.
    """
    put_bytes(uri, json.dumps(page_elements).encode("utf-8"), "application/json")


def _load_page_elements(uri: str) -> dict[int, list[dict]]:
    raw = json.loads(get_bytes(uri).decode("utf-8"))
    return {int(k): v for k, v in raw.items()}


@activity.defn(name="process-pdf_extract-assets")
async def extract_assets_activity(input: ExtractAssetsInput) -> ExtractAssetsOutput:
    activity.heartbeat()

    # Sync body — S3 download + LayoutDetector init + per-page render/yolo/S3-put
    # — easily blocks the event loop past the 30 s heartbeat window. Run it on a
    # worker thread and let await_with_heartbeats tick the activity from the
    # main loop. Same shape gpu_activities uses for vLLM HTTP calls.
    def _run() -> tuple[dict[int, list[dict]], dict[int, str]]:
        all_pages: set[int] = set()
        for start, end in input.page_ranges:
            for p in range(start, end + 1):
                all_pages.add(p)
        with _localized_pdf(input.pdf_path) as local_pdf:
            extractor = AssetExtractor(local_pdf, input.document_id, input.config)
            try:
                return extractor.extract_all_pages(all_pages)
            finally:
                extractor.close()

    page_elements, page_image_uris = await await_with_heartbeats(
        asyncio.to_thread(_run),
    )

    total = sum(len(v) for v in page_elements.values())
    by_type: dict[str, int] = {}
    ocr_targets: list[OcrTarget] = []
    for page_idx, elems in page_elements.items():
        for elem_idx, e in enumerate(elems):
            by_type[e["element_type"]] = by_type.get(e["element_type"], 0) + 1
            if e.get("asset_uri"):
                ocr_targets.append(OcrTarget(
                    page_idx=page_idx,
                    elem_idx=elem_idx,
                    element_id=e["element_id"],
                    asset_uri=e["asset_uri"],
                ))

    uri = _page_elements_uri(input.config, input.document_id, "raw")
    _dump_page_elements(uri, page_elements)

    return ExtractAssetsOutput(
        page_elements_uri=uri,
        page_image_uris=page_image_uris,
        ocr_targets=ocr_targets,
        total_elements=total,
        element_counts=by_type,
    )


@activity.defn(name="process-pdf_attach-ocr")
async def attach_ocr_activity(input: AttachOcrInput) -> AttachOcrOutput:
    """Apply OCR mutations from the workflow to the page_elements dict on S3.

    The workflow can't write to S3 (it's replay-deterministic), so the
    accumulated chandra results round-trip through this activity as the
    `ocr_updates` list. Each update is small (one element's OCR text);
    pathological docs that exceed the 4 MB gRPC ceiling on this single input
    are still possible but would need ~1000+ elements with very long markdown
    output. Acceptable tradeoff vs the dict-in-history shape it replaces.
    """
    activity.heartbeat()
    page_elements = _load_page_elements(input.page_elements_uri)

    # element_id is authoritative — page_idx/elem_idx is a fast-path index.
    # Mismatches (re-extraction reshuffled the dict) skip rather than corrupt.
    for upd in input.ocr_updates:
        elems = page_elements.get(upd.page_idx)
        if not elems or upd.elem_idx >= len(elems):
            continue
        elem = elems[upd.elem_idx]
        if elem.get("element_id") != upd.element_id:
            continue
        if upd.ocr_text is not None:
            elem["ocr_text"] = upd.ocr_text
        if upd.ocr_parsed is not None:
            elem["ocr_parsed"] = upd.ocr_parsed

    enriched = _page_elements_uri(input.config, input.document_id, "enriched")
    _dump_page_elements(enriched, page_elements)
    return AttachOcrOutput(page_elements_uri=enriched)


@activity.defn(name="process-pdf_assign-elements")
async def assign_elements_activity(input: AssignElementsInput) -> AssignElementsOutput:
    page_elements = _load_page_elements(input.page_elements_uri)
    tree = assign_elements_to_tree(
        input.tree, page_elements, input.page_image_uris,
        input.pdf_path, input.config,
    )
    return AssignElementsOutput(tree=tree)


@activity.defn(name="process-pdf_attach-raw-text")
async def attach_raw_text_activity(input: AttachRawTextInput) -> AttachRawTextOutput:
    pages_raw = json.loads(get_bytes(input.pages_uri).decode("utf-8"))
    pages: list[tuple[str, int]] = [(t, n) for t, n in pages_raw]
    return AttachRawTextOutput(tree=attach_raw_text_to_tree(input.tree, pages))


@activity.defn(name="process-pdf_finalize")
async def finalize_activity(input: FinalizeInput) -> FinalizeOutput:
    tree_data = input.tree.model_dump()
    indent = 2 if input.pretty_print else None

    if input.output_path.startswith("s3://"):
        # s3:// URIs are absolute references; no anchor to portablize against.
        # Tree-internal pdf_path / asset_uri values are already s3:// (prod overlay).
        body = json.dumps(tree_data, indent=indent, ensure_ascii=False).encode("utf-8")
        put_bytes(input.output_path, body, "application/json")
        return FinalizeOutput(tree_path=input.output_path)

    tree_path = Path(input.output_path)
    tree_path.parent.mkdir(parents=True, exist_ok=True)
    _portablize_paths(tree_data, tree_path.parent)
    with open(tree_path, "w", encoding="utf-8") as f:
        json.dump(tree_data, f, indent=indent, ensure_ascii=False)
    return FinalizeOutput(tree_path=str(tree_path))


# Indexing route (BM25 / hybrid RAG)

def _index_artifact_uri(config: dict, document_id: str, leaf: str) -> str:
    """Deterministic per-paper index artifact URI. run_id is deliberately absent:
    the user wants one canonical version per paper at rest, so a re-run
    overwrites in place. Prior versions are wiped from OpenSearch by
    wipe_paper in index_chunks_activity."""
    prefix = config["output"]["assets_uri_prefix"].rstrip("/")
    return f"{prefix}/{document_id}/index/{leaf}"


@activity.defn(name="index-document_load-tree")
async def load_tree_activity(input: LoadTreeInput) -> LoadTreeOutput:
    """Read + validate the tree.json from URI; hand upstream config back to the
    workflow so it can call load_pages_activity without a second S3 read.
    """
    raw = get_bytes(input.tree_uri).decode("utf-8")
    tree = DocumentTree.model_validate_json(raw)
    return LoadTreeOutput(
        paper_id=tree.paper_id,
        pdf_path=tree.pdf_path,
        total_pages=tree.total_pages,
        tree_json_uri=input.tree_uri,
    )


@activity.defn(name="index-document_chunk")
async def chunk_document_activity(input: ChunkDocumentInput) -> ChunkDocumentOutput:
    """Load tree + pages, produce chunks, publish to S3.

    Chunk emission is CPU-bound (tiktoken counts inside the tail-overlap
    logic) so runs under asyncio.to_thread to keep the event loop responsive
    for heartbeats.
    """
    activity.heartbeat()

    def _run() -> tuple[list[Chunk], int]:
        tree_raw = get_bytes(input.tree_uri).decode("utf-8")
        tree = DocumentTree.model_validate_json(tree_raw)
        pages_raw = json.loads(get_bytes(input.pages_uri).decode("utf-8"))
        pages: list[tuple[str, int]] = [(t, n) for t, n in pages_raw]

        chunk_cfg = input.config.get("retrieval", {}).get("chunking", {})
        max_tokens = int(chunk_cfg.get("max_tokens", 512))
        overlap_tokens = int(chunk_cfg.get("overlap_tokens", 64))

        chunks = chunk_document(
            tree, pages,
            max_tokens=max_tokens, overlap_tokens=overlap_tokens,
            tree_uri=input.tree_uri,
        )
        return chunks, sum(c.token_count for c in chunks)

    chunks, total_tokens = await await_with_heartbeats(asyncio.to_thread(_run))

    chunks_uri = _index_artifact_uri(input.config, input.document_id, "chunks.json")
    body = json.dumps([c.model_dump(exclude_none=True) for c in chunks]).encode("utf-8")
    put_bytes(chunks_uri, body, "application/json")

    return ChunkDocumentOutput(
        chunks_uri=chunks_uri,
        chunk_count=len(chunks),
        total_tokens=total_tokens,
    )


@activity.defn(name="index-document_index-chunks")
async def index_chunks_activity(input: IndexChunksInput) -> IndexChunksOutput:
    """Read chunks + embeddings from S3, wipe the paper's prior slot in
    OpenSearch, then bulk-index the fresh chunks.

    Aggressive-overwrite semantics: the caller (workflow) owns exactly one
    version per paper. wipe_paper drops any lingering doc_id under this
    paper_id BEFORE the fresh write lands, so a re-run with a different
    chunker output can't leave orphans behind.
    """
    activity.heartbeat()

    def _run() -> int:
        from shared.opensearch import (
            build_client, bulk_index_chunks, ensure_index, wipe_paper,
        )

        chunks_raw = json.loads(get_bytes(input.chunks_uri).decode("utf-8"))
        # embeddings.npy: fp16 array, shape (N, dim), positionally aligned with
        # chunks. np.load reconstructs shape + dtype from the .npy header;
        # allow_pickle=False keeps this to plain arrays only. Upcast to fp32
        # for OpenSearch — the knn_vector field stores float32 regardless.
        emb_arr = np.load(io.BytesIO(get_bytes(input.embeddings_uri)), allow_pickle=False)
        if emb_arr.ndim != 2:
            raise RuntimeError(
                f"embeddings array must be 2-D (N, dim); got shape {emb_arr.shape}"
            )
        if len(chunks_raw) != emb_arr.shape[0]:
            raise RuntimeError(
                f"chunks/embeddings length mismatch: {len(chunks_raw)} vs {emb_arr.shape[0]}"
            )
        emb_arr = emb_arr.astype(np.float32, copy=False)

        dim = int(input.config["embedding_server"]["dimension"])
        if emb_arr.shape[1] != dim:
            raise RuntimeError(
                f"embedding_server.dimension={dim} but stored vectors are "
                f"{emb_arr.shape[1]}-dim; index_name rotation required"
            )

        chunks = [
            Chunk.model_validate({**c, "embedding": emb_arr[i].tolist()})
            for i, c in enumerate(chunks_raw)
        ]

        client = build_client(input.config)
        ensure_index(client, input.index_name, embedding_dim=dim)
        wipe_paper(client, input.index_name, input.paper_id)
        return bulk_index_chunks(client, input.index_name, chunks)

    indexed = await await_with_heartbeats(asyncio.to_thread(_run))
    return IndexChunksOutput(indexed_count=indexed, index_name=input.index_name)


# Registered with workers that poll the CPU lane (BATCH_CPU_TQ / LIVE_CPU_TQ).
# Mis-routed dispatch fails fast ("no handler") instead of running on the
# wrong worker class.
CPU_ACTIVITIES = [
    load_pages_activity,
    extract_assets_activity,
    attach_ocr_activity,
    assign_elements_activity,
    attach_raw_text_activity,
    finalize_activity,
    load_tree_activity,
    chunk_document_activity,
    index_chunks_activity,
]
