"""Smoke test: tree_logic.build_tree with an inline fake call_llm.

Verifies the orchestration runs end-to-end without Temporal or a live model.
The fake responds to prompts by content keywords — designed to drive the
no-TOC happy path through a tiny 4-page document.

Run: python -m tests.test_tree_logic
"""

import asyncio
import random

from etl.pipeline.tree_logic import BuildOpt, LlmResult, build_tree
from shared.schemas import DocumentTree


async def _fake_call_llm(model, prompt, *, json_mode=False, temperature=0.0):
    p = prompt

    if "toc_detected" in p:
        return LlmResult(model=model, content='{"toc_detected": "no"}', finish_reason="stop")

    if "page_index_given_in_toc" in p:
        return LlmResult(
            model=model, content='{"page_index_given_in_toc": "no"}', finish_reason="stop",
        )

    if "main points covered" in p:
        return LlmResult(model=model, content="A concise summary.", finish_reason="stop")

    if "appear or start" in p or "appears or starts" in p:
        return LlmResult(model=model, content='{"answer": "yes"}', finish_reason="stop")

    if "very first content" in p or "starts in the beginning" in p:
        return LlmResult(model=model, content='{"start_begin": "no"}', finish_reason="stop")

    # generate_toc_init upstream — distinctive phrase "extracting hierarchical tree structure"
    if "hierarchical tree structure" in p:
        return LlmResult(
            model=model,
            content=(
                '[{"structure": "1", "title": "Section 1", '
                '"physical_index": "<physical_index_1>"},'
                '{"structure": "2", "title": "Section 2", '
                '"physical_index": "<physical_index_3>"}]'
            ),
            finish_reason="stop",
        )

    return LlmResult(model=model, content="{}", finish_reason="stop")


async def _build():
    pages = [
        ("Section 1\nIntroduction paragraph...", 50),
        ("Continued content of Section 1...", 50),
        ("Section 2\nMain body content...", 50),
        ("More content for Section 2...", 50),
    ]
    opt = BuildOpt(
        model="fake-strong",
        model_fast="fake-fast",
        prompt_style="local",
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
        pages, opt,
        call_llm=_fake_call_llm,
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


if __name__ == "__main__":
    test_build_tree_smoke()
    print("PASS: tree_logic smoke test")
