"""Smoke test: tree_logic.build_tree end-to-end with an inline fake call_llm."""

import asyncio
import json
import random
import tempfile
from pathlib import Path

from pipeline.tree_logic import BuildOpt, LlmResult, build_tree, collapse_over_decomposed_leaves
from shared.schemas import DocumentTree
from shared.temporal.activity_models import PromptSpec


class _FakeCallLlm:
    """Fake CallLlm matching the class-shape protocol (__call__ + parsed).

    Text calls (`__call__`) return prose for summary-generation paths.
    Structured calls (`parsed`) dispatch on `response_schema` key — dicts here
    mirror what the production `llm_structured_call_activity` would emit after
    instructor validation.
    """

    async def __call__(self, model, spec: PromptSpec, *, json_mode=False, temperature=0.0):
        return LlmResult(model=model, content="A concise summary.", finish_reason="stop")

    async def parsed(self, model, spec: PromptSpec, response_schema: str, *, temperature=0.0):
        if response_schema == "toc_detection":
            return {"toc_detected": "no"}
        if response_schema == "page_index_present":
            return {"page_index_given_in_toc": "no"}
        if response_schema == "toc_completion":
            return {"completed": "yes"}
        if response_schema == "toc_list":
            return {
                "toc": [
                    {"structure": "1", "title": "Section 1",
                     "physical_index": "<physical_index_1>"},
                    {"structure": "2", "title": "Section 2",
                     "physical_index": "<physical_index_3>"},
                ],
            }
        if response_schema == "title_appearance":
            return {"answer": "yes"}
        if response_schema == "title_starts_section":
            return {"start_begin": "no"}
        if response_schema == "physical_index_fix":
            return {"physical_index": None}
        if response_schema == "summary_verdict":
            return {"faithful": "yes", "missed_topics": []}
        if response_schema == "abstract":
            return {"abstract": None}
        return {}


async def _build():
    pages = [
        ("Section 1\nIntroduction paragraph...", 50),
        ("Continued content of Section 1...", 50),
        ("Section 2\nMain body content...", 50),
        ("More content for Section 2...", 50),
    ]
    token_counts = [t for _, t in pages]

    with tempfile.TemporaryDirectory() as tmp:
        pages_uri = str(Path(tmp) / "pages.json")
        Path(pages_uri).write_text(json.dumps(pages))

        opt = BuildOpt(
            model="fake-strong",
            model_fast="fake-fast",
            prompt_style="local",
            pages_uri=pages_uri,
            toc_check_page_num=3,
            max_page_num_each_node=20,
            max_token_num_each_node=10_000,
            if_add_node_id="yes",
            if_add_node_summary="yes",
            if_add_doc_description="no",
            summary_overlap_pages=0,
            verify_summaries=False,
        )

        return await build_tree(
            token_counts, opt,
            call_llm=_FakeCallLlm(),
            rng=random.Random(0),
            paper_id="fake-doc",
            pdf_path="/tmp/fake.pdf",
        )


def test_build_tree_smoke():
    tree = asyncio.run(_build())
    assert isinstance(tree, DocumentTree)
    assert tree.paper_id == "fake-doc"
    assert tree.pdf_path == "/tmp/fake.pdf"
    assert tree.total_pages == 4
    assert tree.root_nodes, "expected at least one root node"
    assert tree.root_nodes[0].node_id == "0000"
    assert tree.root_nodes[0].summary, "expected node summary populated"


def test_collapse_over_decomposed_leaves():
    body = {"title": "Introduction", "start_index": 1, "end_index": 5, "nodes": []}
    citations = [
        {"title": "S. Y. Sayed, K. P. Yao, D. G. Kwabi, ...",     "start_index": 48, "end_index": 48},
        {"title": "T. Ogasawara, A. Debart, M. Holzapfel, ...",   "start_index": 48, "end_index": 48},
        {"title": "F. Mizuno, S. Nakanishi, Y. Kotani, ...",      "start_index": 48, "end_index": 48},
        {"title": "S. A. Freunberger, Y. Chen, Z. Peng, ...",     "start_index": 48, "end_index": 48},
        {"title": "V. S. Bryantsev, V. Giordani, W. Walker, ...", "start_index": 48, "end_index": 48},
        {"title": "M. Zhang, Y. Li, Nano Energy, 2020, 6, 15",    "start_index": 49, "end_index": 49},
    ]
    tree = [body] + citations
    out = collapse_over_decomposed_leaves(tree)
    assert len(out) == 2, f"expected [Intro, References], got {len(out)}"
    assert out[0]["title"] == "Introduction"
    assert out[1]["title"] == "References"
    assert out[1]["start_index"] == 48
    assert out[1]["end_index"] == 49
    assert out[1]["nodes"] == []

    # Short run stays intact (real subsections might have 2-3 initial-shaped titles).
    short = [
        {"title": "F. Rocket, Analysis, 2022", "start_index": 10, "end_index": 10},
        {"title": "G. Mars, Journal, 2023",    "start_index": 11, "end_index": 11},
    ]
    assert collapse_over_decomposed_leaves(short) == short, "must not collapse < min-run"

    # Idempotent.
    twice = collapse_over_decomposed_leaves(collapse_over_decomposed_leaves(tree))
    assert twice == out


if __name__ == "__main__":
    test_build_tree_smoke()
    print("PASS: tree_logic smoke test")
    test_collapse_over_decomposed_leaves()
    print("PASS: collapse_over_decomposed_leaves")
