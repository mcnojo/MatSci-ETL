"""CPU-lane Temporal activities + their I/O models.

The four stage-level activities — load_pages, extract_assets, assign_elements,
finalize — plus the Pydantic I/O models they share with the workflow. Pulls
the heavy CV/ML stack (torch, doclayout-yolo, opencv) transitively via
AssetExtractor; lives behind the pipeline-cpu extra and is only loaded by
workers that register the CPU-lane queues or the workflows that fan out to
those activities.
"""

import asyncio
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pymupdf
from pydantic import BaseModel, ConfigDict
from temporalio import activity

from shared.s3_io import get_bytes
from shared.schemas import DocumentTree

from .asset_extractor import AssetExtractor
from .enricher import assign_elements_to_tree
from .heartbeat import await_with_heartbeats
from .tree_logic import count_tokens


# I/O models

class LoadPagesInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    pdf_path: str


class LoadPagesOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    page_list: list[tuple[str, int]]  # (page_text, token_count) per physical page
    total_pages: int
    is_likely_scanned: bool


class ExtractAssetsInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    pdf_path: str
    document_id: str
    run_id: str
    config: dict
    page_ranges: list[tuple[int, int]]


class ExtractAssetsOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    page_elements: dict[int, list[dict]]
    # page_index -> URI of the rendered page PNG. Empty when save_page_images=false.
    # Threaded into AssignElementsInput so the enricher never touches local pages_dir.
    page_image_uris: dict[int, str]
    total_elements: int
    element_counts: dict[str, int]


class AssignElementsInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    tree: DocumentTree
    page_elements: dict[int, list[dict]]
    page_image_uris: dict[int, str]
    pdf_path: str
    document_id: str
    config: dict


class AssignElementsOutput(BaseModel):
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

@activity.defn(name="process-pdf_load-pages")
async def load_pages_activity(input: LoadPagesInput) -> LoadPagesOutput:
    with _localized_pdf(input.pdf_path) as local_pdf:
        page_list = _get_page_tokens(local_pdf)
    return LoadPagesOutput(
        page_list=page_list,
        total_pages=len(page_list),
        is_likely_scanned=_is_likely_scanned(page_list),
    )


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
    for elems in page_elements.values():
        for e in elems:
            by_type[e["element_type"]] = by_type.get(e["element_type"], 0) + 1

    return ExtractAssetsOutput(
        page_elements=page_elements,
        page_image_uris=page_image_uris,
        total_elements=total,
        element_counts=by_type,
    )


@activity.defn(name="process-pdf_assign-elements")
async def assign_elements_activity(input: AssignElementsInput) -> AssignElementsOutput:
    tree = assign_elements_to_tree(
        input.tree, input.page_elements, input.page_image_uris,
        input.pdf_path, input.config,
    )
    return AssignElementsOutput(tree=tree)


@activity.defn(name="process-pdf_finalize")
async def finalize_activity(input: FinalizeInput) -> FinalizeOutput:
    tree_path = Path(input.output_path)
    tree_path.parent.mkdir(parents=True, exist_ok=True)

    tree_data = input.tree.model_dump()
    _portablize_paths(tree_data, tree_path.parent)

    indent = 2 if input.pretty_print else None
    with open(tree_path, "w", encoding="utf-8") as f:
        json.dump(tree_data, f, indent=indent, ensure_ascii=False)
    return FinalizeOutput(tree_path=str(tree_path))


# Registered with workers that poll the CPU lane (BATCH_CPU_TQ / LIVE_CPU_TQ).
# Mis-routed dispatch fails fast ("no handler") instead of running on the
# wrong worker class.
CPU_ACTIVITIES = [
    load_pages_activity,
    extract_assets_activity,
    assign_elements_activity,
    finalize_activity,
]
