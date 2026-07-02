"""Unit tests for pipeline.chunker."""

from pipeline.chunker import (
    _split_text,
    _strip_tags,
    _tail_within_budget,
    _walk_leaves,
    chunk_document,
)
from shared.schemas import (
    BoundingBox,
    DocumentTree,
    TreeNode,
    VisualElement,
)
from shared.schemas.chunk import (
    CHUNKER_VERSION,
    SOURCE_CHANDRA_MD,
    SOURCE_LAYOUT_CAPTION,
    SOURCE_LLM_ABSTRACT,
    SOURCE_PYMUPDF,
)


def _pages(*bodies: str) -> list[tuple[str, int]]:
    """Test helper: (text, token_count) shape used by pipeline internals."""
    return [(b, len(b.split())) for b in bodies]


# _walk_leaves — DFS leaves + accumulated title breadcrumb

def test_walk_leaves_flat():
    nodes = [
        TreeNode(title="A", node_id="a", start_index=1, end_index=1, nodes=[]),
        TreeNode(title="B", node_id="b", start_index=2, end_index=2, nodes=[]),
    ]
    out = [(n.node_id, bc) for n, bc in _walk_leaves(nodes)]
    assert out == [("a", ["A"]), ("b", ["B"])]


def test_walk_leaves_nested():
    nodes = [
        TreeNode(title="Root", node_id="r", start_index=1, end_index=5, nodes=[
            TreeNode(title="Kid", node_id="k", start_index=1, end_index=3, nodes=[
                TreeNode(title="Grand", node_id="g", start_index=1, end_index=2, nodes=[]),
            ]),
            TreeNode(title="Leaf2", node_id="l2", start_index=4, end_index=5, nodes=[]),
        ]),
    ]
    out = [(n.node_id, bc) for n, bc in _walk_leaves(nodes)]
    assert out == [("g", ["Root", "Kid", "Grand"]), ("l2", ["Root", "Leaf2"])]


# _tail_within_budget — whole-sentence trailing overlap

def test_tail_within_budget_zero_returns_empty():
    assert _tail_within_budget("Hello there. Sentence two.", 0) == ""


def test_tail_within_budget_picks_whole_sentences():
    text = "One. Two. Three. Four."
    tail = _tail_within_budget(text, 6)
    # Should end at a sentence boundary (never mid-word), never exceed budget.
    assert tail.endswith(".") or tail == ""
    for s in ("One", "Two", "Three", "Four"):
        assert tail.count(s) <= 1


# _split_text — packing logic

def test_split_short_text_returns_one_part():
    text = "One two three four. Five six seven."
    parts = _split_text(text, max_tokens=100, overlap_tokens=8)
    assert parts == [text.strip()]


def test_split_long_text_produces_multiple_parts_with_overlap():
    body = "Sentence one. Sentence two. Sentence three."
    text = "\n\n".join([body] * 20)  # ~200 tokens on cl100k
    parts = _split_text(text, max_tokens=32, overlap_tokens=8)
    assert len(parts) >= 3
    # Overlap: every part after the first should share prefix content with the
    # tail of the previous one (whole sentences carried forward).
    for i in range(1, len(parts)):
        # At least the last sentence of parts[i-1] appears at the head of parts[i]
        prev_tail = parts[i - 1].split(".")[-2].strip()  # last non-empty sentence
        if prev_tail:
            assert prev_tail in parts[i][: len(prev_tail) + 100]


# _strip_tags — defensive belt-and-suspenders against HTML in chunker-visible fields

def test_strip_tags_leaves_plain_text_alone():
    assert _strip_tags("hello world") == "hello world"


def test_strip_tags_removes_table_html():
    html = "<table><tr><td>1</td><td>2</td></tr></table>"
    assert "<" not in _strip_tags(html)


def test_strip_tags_preserves_content_between_tags():
    html = "<p>Sample <b>bold</b> text.</p>"
    stripped = _strip_tags(html)
    assert "Sample" in stripped and "bold" in stripped and "text" in stripped
    assert "<" not in stripped and ">" not in stripped


# chunk_document — end-to-end

def test_chunk_document_emits_abstract_first():
    pages = _pages("body")
    tree = DocumentTree(
        paper_id="p1", pdf_path="p.pdf", total_pages=1,
        abstract="This is an abstract.",
        root_nodes=[TreeNode(title="X", node_id="x", start_index=1, end_index=1, nodes=[])],
    )
    chunks = chunk_document(tree, pages)
    assert chunks[0].kind == "abstract"
    assert chunks[0].node_id == "abstract"
    assert chunks[0].text == "This is an abstract."
    assert chunks[0].source_kind == SOURCE_LLM_ABSTRACT


def test_chunk_document_skips_missing_abstract():
    pages = _pages("body")
    tree = DocumentTree(
        paper_id="p1", pdf_path="p.pdf", total_pages=1, abstract=None,
        root_nodes=[TreeNode(title="X", node_id="x", start_index=1, end_index=1, nodes=[])],
    )
    chunks = chunk_document(tree, pages)
    assert all(c.kind != "abstract" for c in chunks)


def test_chunk_document_walks_only_leaves():
    pages = _pages("intro page.", "methods page.", "results page.")
    tree = DocumentTree(
        paper_id="p1", pdf_path="p.pdf", total_pages=3, abstract=None,
        root_nodes=[
            TreeNode(title="Root", node_id="r", start_index=1, end_index=3, nodes=[
                TreeNode(title="Intro", node_id="i", start_index=1, end_index=1, nodes=[]),
                TreeNode(title="Body", node_id="b", start_index=2, end_index=3, nodes=[
                    TreeNode(title="Methods", node_id="m", start_index=2, end_index=2, nodes=[]),
                    TreeNode(title="Results", node_id="rr", start_index=3, end_index=3, nodes=[]),
                ]),
            ]),
        ],
    )
    chunks = chunk_document(tree, pages)
    leaf_ids = {c.node_id for c in chunks if c.kind == "section_text"}
    assert leaf_ids == {"i", "m", "rr"}   # root & Body are not leaves


def test_chunk_document_stable_doc_ids():
    pages = _pages("body")
    tree = DocumentTree(
        paper_id="p1", pdf_path="p.pdf", total_pages=1, abstract="A.",
        root_nodes=[TreeNode(title="X", node_id="x", start_index=1, end_index=1, nodes=[])],
    )
    first = [c.doc_id for c in chunk_document(tree, pages)]
    second = [c.doc_id for c in chunk_document(tree, pages)]
    assert first == second
    # Format: {paper_id}:{node_id}:{sub_index}
    for doc_id in first:
        parts = doc_id.split(":")
        assert len(parts) == 3
        assert parts[0] == "p1"


def test_chunk_document_visual_elements_emit_table_and_caption():
    pages = _pages("body")
    elem = VisualElement(
        element_id="e1", element_type="table", page_index=1,
        bbox=BoundingBox(x0=0.1, y0=0.1, x1=0.9, y1=0.5),
        asset_uri="s3://.../e1.png",
        caption="Table 1: sample rates",
        structured_data="| a | b |\n|---|---|\n| 1 | 2 |",
    )
    tree = DocumentTree(
        paper_id="p1", pdf_path="p.pdf", total_pages=1, abstract=None,
        root_nodes=[TreeNode(
            title="Results", node_id="r", start_index=1, end_index=1,
            nodes=[], visual_elements=[elem],
        )],
    )
    chunks = chunk_document(tree, pages)
    kinds = [c.kind for c in chunks]
    assert "table" in kinds
    assert "caption" in kinds
    caption_chunk = next(c for c in chunks if c.kind == "caption")
    assert caption_chunk.text == "Table 1: sample rates"
    assert caption_chunk.source_kind == SOURCE_LAYOUT_CAPTION
    table_chunk = next(c for c in chunks if c.kind == "table")
    assert table_chunk.source_kind == SOURCE_CHANDRA_MD


def test_chunk_document_html_in_structured_data_is_stripped():
    """Defensive: if HTML ever leaks into structured_data (enricher normally
    normalizes to markdown), the chunker must not embed raw tags."""
    pages = _pages("body")
    elem = VisualElement(
        element_id="e_html", element_type="table", page_index=1,
        bbox=BoundingBox(x0=0.1, y0=0.1, x1=0.9, y1=0.5),
        asset_uri="s3://.../e_html.png",
        caption=None,
        structured_data="<table><tr><td>alpha</td><td>beta</td></tr></table>",
    )
    tree = DocumentTree(
        paper_id="p1", pdf_path="p.pdf", total_pages=1, abstract=None,
        root_nodes=[TreeNode(
            title="R", node_id="r", start_index=1, end_index=1,
            nodes=[], visual_elements=[elem],
        )],
    )
    chunks = chunk_document(tree, pages)
    table_chunks = [c for c in chunks if c.kind == "table"]
    assert len(table_chunks) == 1
    # Content preserved, tags removed.
    assert "alpha" in table_chunks[0].text
    assert "beta" in table_chunks[0].text
    assert "<" not in table_chunks[0].text
    assert ">" not in table_chunks[0].text


def test_chunk_document_metadata_provenance():
    """Every chunk carries depth, position, tree_uri, chunker_version."""
    pages = _pages("intro", "results")
    tree = DocumentTree(
        paper_id="p1", pdf_path="p.pdf", total_pages=2, abstract="Abs.",
        root_nodes=[
            TreeNode(title="Root", node_id="r", start_index=1, end_index=2, nodes=[
                TreeNode(title="Intro", node_id="i", start_index=1, end_index=1, nodes=[]),
                TreeNode(title="Results", node_id="rr", start_index=2, end_index=2, nodes=[]),
            ]),
        ],
    )
    chunks = chunk_document(tree, pages, tree_uri="s3://x/tree.json")
    positions = [c.position for c in chunks]
    assert positions == list(range(len(chunks)))  # DFS ordinal, dense from 0
    assert all(c.tree_uri == "s3://x/tree.json" for c in chunks)
    assert all(c.chunker_version == CHUNKER_VERSION for c in chunks)
    # abstract: depth 0 (no breadcrumb); leaves: depth 2 (Root > Intro/Results)
    depths_by_kind = {c.kind: c.depth for c in chunks}
    assert depths_by_kind.get("abstract") == 0
    section_depths = {c.depth for c in chunks if c.kind == "section_text"}
    assert section_depths == {2}


def test_chunk_document_section_text_source_is_pymupdf():
    pages = _pages("body text")
    tree = DocumentTree(
        paper_id="p1", pdf_path="p.pdf", total_pages=1, abstract=None,
        root_nodes=[TreeNode(title="X", node_id="x", start_index=1, end_index=1, nodes=[])],
    )
    chunks = chunk_document(tree, pages)
    sec = [c for c in chunks if c.kind == "section_text"]
    assert len(sec) == 1
    assert sec[0].source_kind == SOURCE_PYMUPDF


def test_chunk_document_split_leaf_gets_deterministic_sub_indices():
    body = "para. " * 200                              # long enough to split
    pages = _pages(body)
    tree = DocumentTree(
        paper_id="p1", pdf_path="p.pdf", total_pages=1, abstract=None,
        root_nodes=[TreeNode(title="Big", node_id="big", start_index=1, end_index=1, nodes=[])],
    )
    chunks = chunk_document(tree, pages, max_tokens=64, overlap_tokens=8)
    section = [c for c in chunks if c.kind == "section_text"]
    assert len(section) >= 2
    assert [c.sub_index for c in section] == list(range(len(section)))


def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    return len(tests)


if __name__ == "__main__":
    n = _run_all()
    print(f"PASS: {n} chunker tests")
