"""Tree enrichment: assign visual elements to their owning nodes.

Per-element OCR happens via the vision_ocr_call activity orchestrated by the workflow,
producing the `ocr_text` / `ocr_parsed` fields. 
This module takes the result and grafts elements onto the DocumentTree.
"""

import re
from pathlib import Path

from shared.schemas import TreeNode, DocumentTree, VisualElement, NodeSource

from .chem_extractor import extract_chem_entities, load_seed_entities
from .table_markdown import html_table_to_markdown


def flatten_tree(nodes: list[TreeNode]) -> list[TreeNode]:
    result = []
    for node in nodes:
        result.append(node)
        if node.nodes:
            result.extend(flatten_tree(node.nodes))
    return result


def assign_elements_to_tree(
    tree: DocumentTree,
    page_elements: dict[int, list[dict]],
    page_image_uris: dict[int, str],
    pdf_path: str,
    config: dict,
) -> DocumentTree:
    """Attach visual elements to the deepest tree node whose page range contains them.

    `page_image_uris` is the page_index -> URI mapping produced by AssetExtractor.
    Empty when output.save_page_images is false; the per-node source list is then
    just empty. No filesystem inspection — URIs are authoritative.
    """
    seed_entities = load_seed_entities(
        str(Path(config.get("_config_dir", "config")) / "chem_entities.yaml")
    )
    flat_nodes = flatten_tree(tree.root_nodes)

    # Build page -> deepest node mapping (later = deeper in DFS order)
    page_to_node: dict[int, TreeNode] = {}
    for node in flat_nodes:
        for p in range(node.start_index, node.end_index + 1):
            page_to_node[p] = node

    # Populate NodeSource from the URI map (no per-worker disk check).
    for node in flat_nodes:
        uris = [
            page_image_uris[p]
            for p in range(node.start_index, node.end_index + 1)
            if p in page_image_uris
        ]
        node.source = NodeSource(
            pdf_path=str(Path(pdf_path).resolve()),
            paper_id=tree.paper_id,
            page_image_uris=uris,
        )

    # Assign elements
    run_chem = config["enrichment"]["run_chem_entity_extraction"]
    for page_idx, elements in page_elements.items():
        target_node = page_to_node.get(page_idx)
        if target_node is None:
            continue
        for elem_dict in elements:
            if run_chem:
                combined_text = " ".join(filter(None, [
                    elem_dict.get("ocr_text", ""),
                    elem_dict.get("caption", ""),
                ]))
                elem_dict["chem_entities"] = extract_chem_entities(combined_text, seed_entities)

            # Tables: extract the first <table>…</table> from the vision layout_html
            # and normalize to markdown. structured_data is retrieval-facing text,
            # so raw HTML would poison BM25 + embeddings — hence the conversion here.
            if elem_dict["element_type"] == "table":
                ocr = elem_dict.get("ocr_text") or ""
                m = re.search(r"<table[\s\S]*?</table>", ocr, re.IGNORECASE)
                if m:
                    md = html_table_to_markdown(m.group(0))
                    if md:
                        elem_dict["structured_data"] = md

            target_node.visual_elements.append(VisualElement(**elem_dict))

    return tree


def attach_raw_text_to_tree(
    tree: DocumentTree, pages: list[tuple[str, int]],
) -> DocumentTree:
    """Populate TreeNode.raw_text for every node with the concatenated OCR text
    over [start_index, end_index] (1-based, inclusive). Same page-scoping as
    the summarizer's <<<section-content>>>, so summary faithfulness can be
    checked against the exact source it was derived from.

    Non-leaf raw_text overlaps its children's — accepted for benchmarking
    convenience (any node reads self-contained). Deterministic and idempotent.
    """
    def _walk(node: TreeNode) -> None:
        lo = max(0, node.start_index - 1)
        hi = min(len(pages), node.end_index)
        node.raw_text = "".join(pages[i][0] for i in range(lo, hi))
        for child in node.nodes:
            _walk(child)

    for root in tree.root_nodes:
        _walk(root)
    return tree
