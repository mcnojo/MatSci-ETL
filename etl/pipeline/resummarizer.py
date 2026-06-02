"""Stage E: figure-aware re-summarization of an enriched DocumentTree.

Leaf nodes with visual_elements are re-summarized using their existing
text summary + per-figure OCR text, caption, and chemistry entities.
Parent nodes are then re-summarized bottom-up from their children's
(now figure-aware) summaries. No new OCR calls are made — we reuse what
the enricher produced.

This module runs only when enrichment runs (gated by --skip-enrichment).
"""

import asyncio
import logging
from typing import Iterable

from shared.schemas import TreeNode, DocumentTree, VisualElement

from .prompts import get_prompt
from . import tree_builder

log = logging.getLogger("resummarizer")


def _format_visual_element(ve: VisualElement) -> str:
    parts = [f"[{ve.element_id}] type={ve.element_type}, page={ve.page_index}"]
    if ve.caption:
        parts.append(f"  caption: {ve.caption}")
    if ve.ocr_text:
        parts.append(f"  ocr_text: {ve.ocr_text}")
    if ve.chem_entities:
        parts.append(f"  chem_entities: {', '.join(ve.chem_entities)}")
    return "\n".join(parts)


async def _resummarize_leaf(node: TreeNode) -> None:
    if not node.visual_elements:
        return
    figure_block = "\n\n".join(
        _format_visual_element(ve) for ve in node.visual_elements
    )
    prompt = get_prompt(
        "figure_aware_resummarize", tree_builder._prompt_style,
        title=node.title,
        prior_summary=node.summary or "",
        figure_block=figure_block,
    )
    try:
        new_summary = await tree_builder.llm_acompletion(
            tree_builder._model_name, prompt,
        )
        node.summary = new_summary.strip()
    except Exception as exc:
        log.warning(
            "figure_aware_resummarize failed for node %s ('%s'): %s",
            node.node_id, node.title, exc,
        )


async def _summarize_parent_from_children(node: TreeNode) -> None:
    if not node.nodes:
        return
    child_summaries = "\n\n".join(
        f"[{child.node_id}] {child.title}\n{child.summary or '(no summary)'}"
        for child in node.nodes
    )
    prompt = get_prompt(
        "summarize_from_children", tree_builder._prompt_style,
        title=node.title,
        child_summaries=child_summaries,
    )
    try:
        new_summary = await tree_builder.llm_acompletion(
            tree_builder._model_name_fast, prompt,
        )
        node.summary = new_summary.strip()
    except Exception as exc:
        log.warning(
            "summarize_from_children failed for node %s ('%s'): %s",
            node.node_id, node.title, exc,
        )


def _walk_post_order(nodes: Iterable[TreeNode]):
    for node in nodes:
        yield from _walk_post_order(node.nodes or [])
        yield node


async def resummarize_with_figures(tree: DocumentTree) -> DocumentTree:
    """Re-summarize leaves with figure data; then walk bottom-up, 
    re-summarizing parents from their updated children summaries."""
    leaf_tasks = [
        _resummarize_leaf(n)
        for n in _walk_post_order(tree.root_nodes)
        if not n.nodes
    ]
    if leaf_tasks:
        await asyncio.gather(*leaf_tasks)

    # Parents are serialized in post order so a grandparent sees its parent's updated summary
    # Could later parallelize within a level, but tree depth is small rn so sequential is fine
    for node in _walk_post_order(tree.root_nodes):
        if node.nodes:
            await _summarize_parent_from_children(node)

    return tree
