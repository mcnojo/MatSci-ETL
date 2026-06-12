"""Temporal activity definitions for the PDF pipeline.

Stage-level activities (load_pages, extract_assets, assign_elements, finalize)
and per-call activities (llm_text_call, chandra_vision_call). The workflow
orchestrates between them and drives tree construction via tree_logic.build_tree
with a call_llm closure that schedules llm_text_call_activity per call.
"""

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import TypeVar

import pymupdf
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict
from temporalio import activity

from shared.vllm.resolve import resolve_vllm_url

_T = TypeVar("_T")


async def _await_with_heartbeats(coro, *, interval_s: float = 20.0) -> _T:
    """Await `coro` while emitting Temporal heartbeats every `interval_s`.

    Pattern: shield the underlying task from the periodic-timeout cancellation
    that `wait_for` would otherwise apply, so the network call keeps running
    while we wake up only long enough to call `activity.heartbeat()`. If the
    activity itself is cancelled (e.g. graceful worker shutdown), propagate
    cancellation to the inner task — shield by itself would not.
    """
    task = asyncio.ensure_future(coro)
    try:
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=interval_s)
            except asyncio.TimeoutError:
                activity.heartbeat()
    except BaseException:
        task.cancel()
        raise

from shared.temporal.activity_models import (
    ChandraCallInput,
    ChandraCallOutput,
    LlmTextCallInput,
    LlmTextCallOutput,
)
from shared.schemas import DocumentTree

from .asset_extractor import AssetExtractor
from .enricher import assign_elements_to_tree
from .llm_calls import execute_text_call
from .tree_logic import count_tokens


# I/O models for stage-level activities

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
    total_elements: int
    element_counts: dict[str, int]


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


class FinalizeInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    tree: DocumentTree
    output_path: str
    pretty_print: bool = True


class FinalizeOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    tree_path: str


# PyMuPDF helpers (used only by load_pages_activity)

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


def _image_to_b64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _openai_usage(response) -> tuple[int, int]:
    u = getattr(response, "usage", None)
    if not u:
        return (0, 0)
    return (
        getattr(u, "prompt_tokens", 0) or 0,
        getattr(u, "completion_tokens", 0) or 0,
    )


# Stage-level activities

@activity.defn(name="process-pdf_load-pages")
async def load_pages_activity(input: LoadPagesInput) -> LoadPagesOutput:
    page_list = _get_page_tokens(input.pdf_path)
    return LoadPagesOutput(
        page_list=page_list,
        total_pages=len(page_list),
        is_likely_scanned=_is_likely_scanned(page_list),
    )


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

    return ExtractAssetsOutput(
        page_elements=page_elements, total_elements=total, element_counts=by_type,
    )


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


_PATH_FIELDS: frozenset[str] = frozenset({"pdf_path", "asset_path"})
_PATH_LIST_FIELDS: frozenset[str] = frozenset({"page_images"})


def _portablize_paths(obj, anchor: Path) -> None:
    """In-place: rewrite known path fields to be relative to `anchor`.

    Only touches absolute values in fields known to hold filesystem paths
    (pdf_path, asset_path, page_images). Leaves content fields untouched.
    Already-relative values pass through unchanged.
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


# Per-call activities

@activity.defn(name="process-pdf_llm-text-call")
async def llm_text_call_activity(input: LlmTextCallInput) -> LlmTextCallOutput:
    activity.heartbeat()
    result = await _await_with_heartbeats(
        execute_text_call(
            input.config, input.model, input.prompt,
            json_mode=input.json_mode, temperature=input.temperature,
        ),
    )
    return LlmTextCallOutput(
        model=result["model"],
        content=result["content"],
        finish_reason=result["finish_reason"],
        started_at=result["started_at"],
        ended_at=result["ended_at"],
        input_tokens=result["input_tokens"],
        output_tokens=result["output_tokens"],
    )


@activity.defn(name="process-pdf_chandra-vision-call")
async def chandra_vision_call_activity(input: ChandraCallInput) -> ChandraCallOutput:
    activity.heartbeat()
    # Worker resolves at activity boundary so OCR_VLLM_PREFER_PRIVATE_IP (in-VPC
    # workers) routes over the private network without the CLI knowing or caring.
    base_url = resolve_vllm_url(input.base_url)
    b64 = _image_to_b64(input.image_path)
    client = AsyncOpenAI(base_url=base_url, api_key=input.api_key)
    started_at = time.time()
    response = await _await_with_heartbeats(
        client.chat.completions.create(
            model=input.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text", "text": input.prompt},
                    ],
                },
            ],
            max_tokens=input.max_tokens,
            temperature=0.0,
        ),
    )
    ended_at = time.time()
    in_t, out_t = _openai_usage(response)
    return ChandraCallOutput(
        content=response.choices[0].message.content or "",
        started_at=started_at,
        ended_at=ended_at,
        input_tokens=in_t,
        output_tokens=out_t,
    )


# Registry

activities = [
    load_pages_activity,
    extract_assets_activity,
    assign_elements_activity,
    finalize_activity,
    llm_text_call_activity,
    chandra_vision_call_activity,
]
