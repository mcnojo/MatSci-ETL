"""Temporal activity definitions for the PDF pipeline.

Each activity is a thin wrapper around the core pipeline functions in this
package. Pydantic I/O models define the Temporal serialization contract.
"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from temporalio import activity

from shared.schemas import DocumentTree

from .asset_extractor import AssetExtractor
from .enricher import Enricher, assign_elements_to_tree
from .metrics import PipelineMetrics, set_current_metrics
from .resummarizer import resummarize_with_figures
from .tree_builder import build_tree_async


# I/O models

class BuildTreeInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    pdf_path: str
    document_id: str
    run_id: str
    config: dict


class BuildTreeOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    tree: DocumentTree
    node_count: int
    total_pages: int


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
    total_elements: int
    element_counts: dict[str, int]


class EnrichOcrInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    page_elements: dict[int, list[dict]]
    config: dict


class EnrichOcrOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    page_elements: dict[int, list[dict]]


class AssignElementsInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    tree: DocumentTree
    page_elements: dict[int, list[dict]]
    pdf_path: str
    document_id: str
    config: dict


class AssignElementsOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    tree: DocumentTree


class ResummarizeInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    tree: DocumentTree
    config: dict


class ResummarizeOutput(BaseModel):
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

def _flatten(nodes):
    for n in nodes:
        yield n
        yield from _flatten(n.nodes)


# Activities

@activity.defn(name="process-pdf_build-tree")
async def build_tree_activity(input: BuildTreeInput) -> BuildTreeOutput:
    set_current_metrics(PipelineMetrics(input.document_id, input.run_id))
    try:
        tree = await build_tree_async(input.pdf_path, input.config)
        node_count = sum(1 for _ in _flatten(tree.root_nodes))
        return BuildTreeOutput(tree=tree, node_count=node_count, total_pages=tree.total_pages)
    finally:
        set_current_metrics(None)


@activity.defn(name="process-pdf_extract-assets")
async def extract_assets_activity(input: ExtractAssetsInput) -> ExtractAssetsOutput:
    all_pages: set[int] = set()
    for start, end in input.page_ranges:
        for p in range(start, end + 1):
            all_pages.add(p)

    extractor = AssetExtractor(input.pdf_path, input.document_id, input.config)
    try:
        page_elements = extractor.extract_all_pages(all_pages)
    finally:
        extractor.close()

    total = sum(len(v) for v in page_elements.values())
    by_type: dict[str, int] = {}
    for elems in page_elements.values():
        for e in elems:
            by_type[e["element_type"]] = by_type.get(e["element_type"], 0) + 1

    return ExtractAssetsOutput(page_elements=page_elements, total_elements=total, element_counts=by_type)


@activity.defn(name="process-pdf_enrich-ocr")
async def enrich_ocr_activity(input: EnrichOcrInput) -> EnrichOcrOutput:
    enricher = Enricher(input.config)
    page_elements = await enricher.enrich_all(input.page_elements, input.config)
    return EnrichOcrOutput(page_elements=page_elements)


@activity.defn(name="process-pdf_assign-elements")
async def assign_elements_activity(input: AssignElementsInput) -> AssignElementsOutput:
    pages_dir = (
        Path(input.config["output"]["kb_root"])
        / input.document_id
        / "assets"
        / "pages"
    )
    tree = assign_elements_to_tree(
        input.tree, input.page_elements, input.pdf_path, pages_dir, input.config,
    )
    return AssignElementsOutput(tree=tree)


@activity.defn(name="process-pdf_resummarize")
async def resummarize_activity(input: ResummarizeInput) -> ResummarizeOutput:
    tree = await resummarize_with_figures(input.tree)
    return ResummarizeOutput(tree=tree)


@activity.defn(name="process-pdf_finalize")
async def finalize_activity(input: FinalizeInput) -> FinalizeOutput:
    tree_path = Path(input.output_path)
    tree_path.parent.mkdir(parents=True, exist_ok=True)
    indent = 2 if input.pretty_print else None
    with open(tree_path, "w", encoding="utf-8") as f:
        json.dump(input.tree.model_dump(), f, indent=indent, ensure_ascii=False)
    return FinalizeOutput(tree_path=str(tree_path))


# Registry

activities = [
    build_tree_activity,
    extract_assets_activity,
    enrich_ocr_activity,
    assign_elements_activity,
    resummarize_activity,
    finalize_activity,
]
