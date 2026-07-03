"""Unit tests for attach_raw_text_to_tree — deterministic CPU pass that
populates TreeNode.raw_text from the assembled OCR page corpus."""

from pipeline.enricher import attach_raw_text_to_tree
from shared.schemas import DocumentTree, TreeNode


def _tree(root_nodes: list[TreeNode]) -> DocumentTree:
    return DocumentTree(
        paper_id="p", pdf_path="/tmp/p.pdf", total_pages=len(root_nodes) + 1,
        root_nodes=root_nodes,
    )


def test_attach_covers_leaf_page_range():
    pages = [("PAGE-1 ", 1), ("PAGE-2 ", 2), ("PAGE-3 ", 3)]
    leaf = TreeNode(title="Intro", node_id="0", start_index=1, end_index=2)
    attach_raw_text_to_tree(_tree([leaf]), pages)
    assert leaf.raw_text == "PAGE-1 PAGE-2 "


def test_attach_recurses_into_children():
    pages = [("A", 1), ("B", 2), ("C", 3)]
    child = TreeNode(title="c", node_id="0.0", start_index=2, end_index=2)
    parent = TreeNode(title="p", node_id="0", start_index=1, end_index=3, nodes=[child])
    attach_raw_text_to_tree(_tree([parent]), pages)
    assert parent.raw_text == "ABC"
    assert child.raw_text == "B"


def test_attach_clamps_out_of_range_indices():
    # Guard: a bogus end_index past the doc still produces a defined string
    # instead of crashing, so a mis-terminated tree doesn't wedge the pipeline.
    pages = [("only-page", 1)]
    node = TreeNode(title="bad", node_id="0", start_index=1, end_index=99)
    attach_raw_text_to_tree(_tree([node]), pages)
    assert node.raw_text == "only-page"


def test_attach_is_idempotent():
    pages = [("X", 1), ("Y", 2)]
    node = TreeNode(title="t", node_id="0", start_index=1, end_index=2)
    tree = _tree([node])
    attach_raw_text_to_tree(tree, pages)
    first = node.raw_text
    attach_raw_text_to_tree(tree, pages)
    assert node.raw_text == first == "XY"


if __name__ == "__main__":
    test_attach_covers_leaf_page_range()
    test_attach_recurses_into_children()
    test_attach_clamps_out_of_range_indices()
    test_attach_is_idempotent()
    print("PASS: attach_raw_text_to_tree")
