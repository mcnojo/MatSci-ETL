"""Tree enrichment: assign visual elements to their owning nodes.

Per-element OCR happens via the chandra_vision_call activity orchestrated by the workflow,
producing the `ocr_text` / `ocr_parsed` fields. 
This module takes the result and grafts elements onto the DocumentTree.
"""

import re
from pathlib import Path

from shared.schemas import TreeNode, DocumentTree, VisualElement, NodeSource

from .chem_extractor import extract_chem_entities, load_seed_entities


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
    pdf_path: str,
    pages_dir: Path,
    config: dict,
) -> DocumentTree:
    """Attach visual elements to the deepest tree node whose page range contains them."""
    seed_entities = load_seed_entities(
        str(Path(config.get("_config_dir", "config")) / "chem_entities.yaml")
    )
    flat_nodes = flatten_tree(tree.root_nodes)

    # Build page -> deepest node mapping (later = deeper in DFS order)
    page_to_node: dict[int, TreeNode] = {}
    for node in flat_nodes:
        for p in range(node.start_index, node.end_index + 1):
            page_to_node[p] = node

    # Populate NodeSource
    save_pages = config["output"]["save_page_images"]
    for node in flat_nodes:
        page_image_paths = []
        if save_pages:
            for p in range(node.start_index, node.end_index + 1):
                img_path = pages_dir / f"p{p:04d}.png"
                if img_path.exists():
                    page_image_paths.append(str(img_path.resolve()))
        node.source = NodeSource(
            pdf_path=str(Path(pdf_path).resolve()),
            paper_id=tree.paper_id,
            page_images=page_image_paths,
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

            # For tables: pull the first <table>…</table> out of chandra's layout_html OCR.
            if elem_dict["element_type"] == "table":
                ocr = elem_dict.get("ocr_text") or ""
                m = re.search(r"<table[\s\S]*?</table>", ocr, re.IGNORECASE)
                if m:
                    elem_dict["structured_data"] = m.group(0)

            target_node.visual_elements.append(VisualElement(**elem_dict))

    return tree
