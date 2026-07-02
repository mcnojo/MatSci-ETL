"""Section-aware chunker: DocumentTree + page text -> list[Chunk].

Walks leaves (nodes with no children) and emits one primary chunk per leaf.
Leaves longer than max_tokens are split into sub-chunks with a token-anchored
overlap on paragraph boundaries. Structured artifacts on the leaf (abstract,
table markdown, figure captions) become their own chunks with a distinct
`kind` so retrievers can filter or weight them separately.

Pure library — no IO, no Temporal. Runs inside a CPU activity.
"""

from __future__ import annotations

import re
from typing import Iterator

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

# Belt-and-suspenders. `enricher._html_table_to_markdown` produces clean
# markdown for the table path, so this is a defensive net in case a future
# code path lets HTML leak into a chunker-visible field.
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    """Drop any lingering HTML tags. Whitespace preserved otherwise."""
    return _TAG_RE.sub("", text)


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

    chunks: list[Chunk] = []
    position = 0  # global DFS ordinal — stable across identical trees

    def emit(*, node_id: str, node_title: str, breadcrumb: list[str],
             page_start: int, page_end: int, kind: str, source_kind: str,
             text: str) -> None:
        nonlocal position
        parts = _split_into_parts(
            text, max_tokens=max_tokens, overlap_tokens=overlap_tokens,
        )
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


def _split_into_parts(
    text: str, *, max_tokens: int, overlap_tokens: int,
) -> list[str]:
    """One leaf's text -> one or more parts.

    Fast path: fits in max_tokens -> single part.
    Slow path: paragraph-anchored split with token-budgeted overlap.
    """
    if count_tokens(text) <= max_tokens:
        return [text]
    return _split_text(text, max_tokens=max_tokens, overlap_tokens=overlap_tokens)


def _split_text(text: str, *, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Paragraph-anchored greedy packing with tail-overlap.

    Splits on blank lines first; if a single paragraph exceeds max_tokens, falls
    back to sentence splits inside that paragraph. Each emitted part starts with
    up to `overlap_tokens` of tail-carry from the previous part (whole sentences,
    never mid-word), so a phrase straddling a boundary stays retrievable.
    """
    units = _paragraph_units(text)
    parts: list[str] = []
    current: list[str] = []
    current_tokens = 0
    tail_carry = ""  # rendered text (already tokenized) staged for next part's head

    def flush() -> None:
        nonlocal current, current_tokens, tail_carry
        if not current:
            return
        body = "\n\n".join(current).strip()
        rendered = (tail_carry + "\n\n" + body) if tail_carry else body
        parts.append(rendered)
        tail_carry = _tail_within_budget(body, overlap_tokens)
        current = []
        current_tokens = 0

    for unit in units:
        unit_tokens = count_tokens(unit)
        if unit_tokens > max_tokens:
            flush()
            for sub in _sentence_split_over_budget(unit, max_tokens=max_tokens, overlap_tokens=overlap_tokens):
                parts.append(sub)
                tail_carry = _tail_within_budget(sub, overlap_tokens)
            continue
        # tail_carry is prepended at flush; account for it against the budget.
        carry_tokens = count_tokens(tail_carry) if tail_carry and not current else 0
        if current_tokens + unit_tokens + carry_tokens > max_tokens and current:
            flush()
        current.append(unit)
        current_tokens += unit_tokens

    flush()
    return parts


def _paragraph_units(text: str) -> list[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def _sentence_split_over_budget(
    text: str, *, max_tokens: int, overlap_tokens: int,
) -> list[str]:
    """Pack sentence-by-sentence. Any single sentence beyond the budget goes
    through verbatim (splitting mid-sentence corrupts retrieval more than
    over-shooting a soft cap).
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    parts: list[str] = []
    cur: list[str] = []
    cur_tokens = 0
    tail = ""
    for s in sentences:
        st = count_tokens(s)
        carry = count_tokens(tail) if tail and not cur else 0
        if cur and cur_tokens + st + carry > max_tokens:
            body = " ".join(cur)
            parts.append((tail + " " + body).strip() if tail else body)
            tail = _tail_within_budget(body, overlap_tokens)
            cur = []
            cur_tokens = 0
        cur.append(s)
        cur_tokens += st
    if cur:
        body = " ".join(cur)
        parts.append((tail + " " + body).strip() if tail else body)
    return parts


def _tail_within_budget(text: str, overlap_tokens: int) -> str:
    """Return the trailing suffix of `text` that fits in overlap_tokens,
    truncated on whole-sentence boundaries so partial phrases don't leak.
    """
    if overlap_tokens <= 0 or not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    tail: list[str] = []
    running = 0
    for s in reversed(sentences):
        st = count_tokens(s)
        if running + st > overlap_tokens:
            break
        tail.insert(0, s)
        running += st
    return " ".join(tail).strip()


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
