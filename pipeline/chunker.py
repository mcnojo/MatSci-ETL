"""Section-aware chunker: DocumentTree + page text -> list[Chunk].

Walks leaves and emits one primary chunk per leaf; oversized leaves are split
by semantic-text-splitter (tiktoken cl100k_base, paragraph→sentence→word→char
cascade, token-budget overlap). Structured artifacts (abstract, table markdown,
figure captions) become their own chunks with distinct `kind` so retrievers can
filter or weight them separately.

Pure library — no IO, no Temporal. Runs inside a CPU activity.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterator

from semantic_text_splitter import TextSplitter

from shared.schemas import Chunk, DocumentTree, TreeNode
from shared.schemas.chunk import (
    CHUNKER_VERSION,
    KIND_ABSTRACT,
    KIND_CAPTION,
    KIND_SECTION_TEXT,
    KIND_TABLE,
    SOURCE_CHANDRA_MD,
    SOURCE_LAYOUT_CAPTION,
    SOURCE_LLM_ABSTRACT,
    SOURCE_PYMUPDF,
)

from .tree_logic import count_tokens


DEFAULT_MAX_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 64
_ABSTRACT_NODE_ID = "abstract"

# Belt-and-suspenders: enricher normalizes tables to markdown, so this only
# fires if a future code path lets HTML leak into a chunker-visible field.
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    """Drop any lingering HTML tags. Whitespace preserved otherwise."""
    return _TAG_RE.sub("", text)


@lru_cache(maxsize=16)
def _splitter(max_tokens: int, overlap_tokens: int) -> TextSplitter:
    # gpt-4 == cl100k_base, matching tree_logic.count_tokens — keeps chunk
    # boundaries and the emitted token_count consistent. Cached per (cap,
    # overlap) since worker processes reuse the config across documents.
    return TextSplitter.from_tiktoken_model(
        "gpt-4", max_tokens, overlap=overlap_tokens,
    )


def chunk_document(
    tree: DocumentTree,
    pages: list[tuple[str, int]],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    tree_uri: str | None = None,
) -> list[Chunk]:
    """Emit a deterministic list of chunks for `tree`.

    Order: abstract (if present) -> leaf chunks in DFS order -> visual-element
    chunks (tables, captions) per leaf. `doc_id` is stable across runs; a
    re-index overwrites in place. `tree_uri` is stamped on every emitted chunk
    for traceback; None is legal for tree-in-memory tests.
    """
    if max_tokens <= overlap_tokens:
        raise ValueError(
            f"max_tokens ({max_tokens}) must exceed overlap_tokens ({overlap_tokens})"
        )
    splitter = _splitter(max_tokens, overlap_tokens)

    chunks: list[Chunk] = []
    position = 0  # global DFS ordinal — stable across identical trees

    def emit(*, node_id: str, node_title: str, breadcrumb: list[str],
             page_start: int, page_end: int, kind: str, source_kind: str,
             text: str) -> None:
        nonlocal position
        # splitter.chunks returns [] on empty input and a single-element list
        # for anything already under the budget — no fast-path branch needed.
        parts = splitter.chunks(text) if text else []
        for sub_index, part in enumerate(parts):
            chunks.append(_mk(
                paper_id=tree.paper_id, node_id=node_id, sub_index=sub_index,
                node_title=node_title, breadcrumb=breadcrumb,
                page_start=page_start, page_end=page_end,
                kind=kind, source_kind=source_kind,
                text=part, token_count=count_tokens(part),
                position=position, tree_uri=tree_uri,
            ))
            position += 1

    if tree.abstract:
        emit(
            node_id=_ABSTRACT_NODE_ID, node_title="Abstract", breadcrumb=[],
            page_start=1, page_end=min(3, tree.total_pages),
            kind=KIND_ABSTRACT, source_kind=SOURCE_LLM_ABSTRACT,
            text=tree.abstract,
        )

    for node, breadcrumb in _walk_leaves(tree.root_nodes):
        leaf_text = _extract_leaf_text(node, pages)
        if leaf_text.strip():
            emit(
                node_id=node.node_id, node_title=node.title, breadcrumb=breadcrumb,
                page_start=node.start_index, page_end=node.end_index,
                kind=KIND_SECTION_TEXT, source_kind=SOURCE_PYMUPDF,
                text=leaf_text,
            )
        for elem in node.visual_elements:
            elem_node_id = f"{node.node_id}:{elem.element_id}"
            if elem.structured_data:
                clean = _strip_tags(elem.structured_data).strip()
                if clean:
                    emit(
                        node_id=elem_node_id,
                        node_title=elem.caption or f"{elem.element_type} on p.{elem.page_index}",
                        breadcrumb=breadcrumb,
                        page_start=elem.page_index, page_end=elem.page_index,
                        kind=KIND_TABLE, source_kind=SOURCE_CHANDRA_MD,
                        text=clean,
                    )
            if elem.caption:
                clean = _strip_tags(elem.caption).strip()
                if clean:
                    emit(
                        node_id=elem_node_id,
                        node_title=f"Caption for {elem.element_type} on p.{elem.page_index}",
                        breadcrumb=breadcrumb,
                        page_start=elem.page_index, page_end=elem.page_index,
                        kind=KIND_CAPTION, source_kind=SOURCE_LAYOUT_CAPTION,
                        text=clean,
                    )

    return chunks


def _walk_leaves(
    nodes: list[TreeNode], breadcrumb: list[str] | None = None,
) -> Iterator[tuple[TreeNode, list[str]]]:
    """DFS: yield (leaf, breadcrumb-of-ancestor-titles) for every leaf."""
    breadcrumb = breadcrumb or []
    for node in nodes:
        if node.nodes:
            yield from _walk_leaves(node.nodes, breadcrumb + [node.title])
        else:
            yield node, breadcrumb + [node.title]


def _extract_leaf_text(node: TreeNode, pages: list[tuple[str, int]]) -> str:
    """Physical pages are 1-based, inclusive. `pages` is 0-indexed."""
    lo = max(0, node.start_index - 1)
    hi = min(len(pages), node.end_index)
    return "".join(pages[i][0] for i in range(lo, hi))


def _mk(
    *, paper_id: str, node_id: str, sub_index: int, node_title: str,
    breadcrumb: list[str], page_start: int, page_end: int,
    kind: str, source_kind: str, text: str, token_count: int,
    position: int, tree_uri: str | None,
) -> Chunk:
    return Chunk(
        doc_id=f"{paper_id}:{node_id}:{sub_index}",
        paper_id=paper_id,
        node_id=node_id,
        sub_index=sub_index,
        node_title=node_title,
        breadcrumb=list(breadcrumb),
        depth=len(breadcrumb),
        position=position,
        page_start=page_start,
        page_end=page_end,
        kind=kind,
        source_kind=source_kind,
        text=text,
        token_count=token_count,
        tree_uri=tree_uri,
        chunker_version=CHUNKER_VERSION,
    )
