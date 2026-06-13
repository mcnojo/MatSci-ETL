"""ProcessPdfWorkflow — orchestrates the PDF pipeline via Temporal.

Stage layout:
  0. load_pages                     coarse-grained activity (PyMuPDF)
  1. tree build                     in-workflow orchestration via tree_logic
                                    + llm_text_call_activity per call
  2. extract_assets                 coarse-grained activity
  3. chandra_vision_call            per-image activity, fanned out
  4. assign_elements                coarse-grained activity
  5. llm_text_call (re-summarize)   per-node activity, fanned out
  6. finalize                       coarse-grained activity

The workflow drives the tree-building orchestration itself. Every LLM call
inside that orchestration is its own Temporal activity, providing fine-grained
visibility, retries, and concurrency control via the worker's
max_concurrent_activities cap.
"""

import asyncio
from pathlib import Path

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from etl.pipeline.activities import (
        AssignElementsInput,
        ExtractAssetsInput,
        FinalizeInput,
        LoadPagesInput,
        assign_elements_activity,
        chandra_vision_call_activity,
        extract_assets_activity,
        finalize_activity,
        llm_text_call_activity,
        load_pages_activity,
    )
    from etl.pipeline.chandra_parser import parse as parse_chandra
    from etl.pipeline.metrics import empty_summary, finalize_summary, merge_call_record
    from etl.pipeline.tree_logic import (
        LlmResult,
        build_opt_from_config,
        build_tree,
    )
    from shared.temporal.activity_models import (
        ChandraCallInput,
        LlmTextCallInput,
    )
    from shared.temporal.task_queues import (
        BATCH_CPU_TQ,
        BATCH_GPU_TQ,
        CPU_ACTIVITY_TIMEOUT,
        CPU_HEARTBEAT_TIMEOUT,
        DEFAULT_RETRY_POLICY,
        GPU_ACTIVITY_TIMEOUT,
        GPU_HEARTBEAT_TIMEOUT,
        GPU_RETRY_POLICY,
        LIVE_CPU_TQ,
        LIVE_GPU_TQ,
    )
    from shared.prompts.chandra import CHANDRA_OCR_LAYOUT_PROMPT
    from shared.prompts.etl import get_prompt
    from shared.schemas import TreeNode, VisualElement

    from .models import (
        ProcessPdfWorkflowInput,
        ProcessPdfWorkflowOutput,
    )


# Motif self-detection: ProcessPdfWorkflow runs in both live and batch modes.
# The caller starts it on a CPU queue (LIVE_CPU_TQ or BATCH_CPU_TQ); the
# matching GPU sibling is derived from there. Keeps motif decisions out of
# the input model — the queue it was scheduled on IS the motif signal.
_GPU_SIBLING = {
    LIVE_CPU_TQ: LIVE_GPU_TQ,
    BATCH_CPU_TQ: BATCH_GPU_TQ,
}


def _flatten(nodes):
    for n in nodes:
        yield n
        yield from _flatten(n.nodes)


def _walk_post_order(nodes):
    for n in nodes:
        yield from _walk_post_order(n.nodes or [])
        yield n


def _format_visual_element(ve: VisualElement) -> str:
    parts = [f"[{ve.element_id}] type={ve.element_type}, page={ve.page_index}"]
    if ve.caption:
        parts.append(f"  caption: {ve.caption}")
    if ve.ocr_text:
        parts.append(f"  ocr_text: {ve.ocr_text}")
    if ve.chem_entities:
        parts.append(f"  chem_entities: {', '.join(ve.chem_entities)}")
    return "\n".join(parts)


@workflow.defn
class ProcessPdfWorkflow:
    """Durable orchestrator for the PDF -> document tree pipeline."""

    @workflow.run
    async def run(self, input: ProcessPdfWorkflowInput) -> ProcessPdfWorkflowOutput:
        config = input.config
        summary = empty_summary(input.document_id, input.run_id)

        cpu_q = workflow.info().task_queue
        gpu_q = _GPU_SIBLING.get(cpu_q)
        if gpu_q is None:
            raise ApplicationError(
                f"ProcessPdfWorkflow started on unrecognized task queue "
                f"{cpu_q!r}; expected one of {sorted(_GPU_SIBLING)}",
                non_retryable=True,
            )

        # Stage 0: Load pages (CPU)
        load_out = await workflow.execute_activity(
            load_pages_activity,
            LoadPagesInput(pdf_path=input.pdf_path),
            task_queue=cpu_q,
            start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
            heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        pages = load_out.page_list
        total_pages = load_out.total_pages

        if load_out.is_likely_scanned:
            workflow.logger.warning(
                "PDF appears to be scanned (low extractable text). "
                "Tree quality may be poor — consider OCR preprocessing."
            )

        # Stage 1: Build tree — workflow drives orchestration, every LLM call is a Temporal activity
        opt = build_opt_from_config(config)
        call_llm = self._make_call_llm(config, summary, gpu_q)
        tree = await build_tree(
            pages, opt,
            call_llm=call_llm,
            rng=workflow.random(),
            paper_id=input.document_id,
            pdf_path=input.pdf_path,
        )
        node_count = sum(1 for _ in _flatten(tree.root_nodes))

        if input.skip_enrichment:
            return await self._finalize(input, config, tree, summary, node_count, total_pages, cpu_q)

        # Stage 2: Extract visual assets (CPU)
        page_ranges = [
            (n.start_index, n.end_index) for n in _flatten(tree.root_nodes)
        ]
        assets_out = await workflow.execute_activity(
            extract_assets_activity,
            ExtractAssetsInput(
                pdf_path=input.pdf_path,
                document_id=input.document_id,
                run_id=input.run_id,
                config=config,
                page_ranges=page_ranges,
            ),
            task_queue=cpu_q,
            start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
            heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        page_elements = assets_out.page_elements

        # Stage 3: Per-image Chandra OCR (GPU) — fan out
        if config["enrichment"]["run_ocr"]:
            await self._enrich_ocr(page_elements, config, summary, gpu_q)

        # Stage 4: Assign elements to tree (CPU)
        assign_out = await workflow.execute_activity(
            assign_elements_activity,
            AssignElementsInput(
                tree=tree,
                page_elements=page_elements,
                pdf_path=input.pdf_path,
                document_id=input.document_id,
                config=config,
            ),
            task_queue=cpu_q,
            start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
            heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        tree = assign_out.tree

        # Stage 5: Figure-aware re-summarization (GPU) — per-node fan out + post-order sweep
        if config.get("enrichment", {}).get("figure_aware_resummarize", True):
            await self._resummarize(tree, config, summary, gpu_q)

        # Stage 6: Finalize
        return await self._finalize(input, config, tree, summary, node_count, total_pages, cpu_q)

    def _make_call_llm(self, config: dict, summary: dict, gpu_q: str):
        """Closure that schedules llm_text_call_activity and merges metrics."""
        async def call(model, prompt, *, json_mode=False, temperature=0.0) -> LlmResult:
            result = await workflow.execute_activity(
                llm_text_call_activity,
                LlmTextCallInput(
                    config=config, model=model, prompt=prompt,
                    json_mode=json_mode, temperature=temperature,
                ),
                task_queue=gpu_q,
                start_to_close_timeout=GPU_ACTIVITY_TIMEOUT,
                heartbeat_timeout=GPU_HEARTBEAT_TIMEOUT,
                retry_policy=GPU_RETRY_POLICY,
            )
            merge_call_record(
                summary, result.model,
                started_at=result.started_at,
                ended_at=result.ended_at,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
            return LlmResult(
                model=result.model,
                content=result.content,
                finish_reason=result.finish_reason,
            )
        return call

    async def _enrich_ocr(
        self, page_elements: dict[int, list[dict]], config: dict, summary: dict, gpu_q: str,
    ) -> None:
        vision_cfg = config["vision_server"]
        ocr_model_label = f"ocr:{vision_cfg['ocr_model']}"

        targets: list[tuple[int, int]] = []
        for page_idx, elements in page_elements.items():
            for elem_idx, elem in enumerate(elements):
                if elem.get("asset_path"):
                    targets.append((page_idx, elem_idx))

        if not targets:
            return

        async def call_one(page_idx: int, elem_idx: int):
            elem = page_elements[page_idx][elem_idx]
            try:
                result = await workflow.execute_activity(
                    chandra_vision_call_activity,
                    ChandraCallInput(
                        base_url=vision_cfg["base_url"],
                        api_key=vision_cfg["api_key"],
                        model=vision_cfg["ocr_model"],
                        image_path=elem["asset_path"],
                        prompt=CHANDRA_OCR_LAYOUT_PROMPT,
                    ),
                    task_queue=gpu_q,
                    start_to_close_timeout=GPU_ACTIVITY_TIMEOUT,
                    heartbeat_timeout=GPU_HEARTBEAT_TIMEOUT,
                    retry_policy=GPU_RETRY_POLICY,
                )
            except Exception as exc:
                elem["ocr_text"] = f"ERROR: {exc}"
                merge_call_record(summary, ocr_model_label, errored=True)
                return
            elem["ocr_text"] = result.content
            elem["ocr_parsed"] = parse_chandra(result.content)
            merge_call_record(
                summary, ocr_model_label,
                started_at=result.started_at,
                ended_at=result.ended_at,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )

        async with asyncio.TaskGroup() as tg:
            for page_idx, elem_idx in targets:
                tg.create_task(call_one(page_idx, elem_idx))

    async def _resummarize(
        self, tree, config: dict, summary: dict, gpu_q: str,
    ) -> None:
        text_cfg = config["tree_llm"]
        prompt_style = (text_cfg.get("prompt_style") or "local").lower()
        model_strong = text_cfg["model"]
        model_fast = text_cfg.get("model_fast") or model_strong

        leaf_targets: list[TreeNode] = [
            n for n in _walk_post_order(tree.root_nodes)
            if not n.nodes and n.visual_elements
        ]

        async def resum_leaf(node: TreeNode):
            figure_block = "\n\n".join(
                _format_visual_element(ve) for ve in node.visual_elements
            )
            prompt = get_prompt(
                "figure_aware_resummarize", prompt_style,
                title=node.title,
                prior_summary=node.summary or "",
                figure_block=figure_block,
            )
            try:
                result = await workflow.execute_activity(
                    llm_text_call_activity,
                    LlmTextCallInput(config=config, model=model_strong, prompt=prompt),
                    task_queue=gpu_q,
                    start_to_close_timeout=GPU_ACTIVITY_TIMEOUT,
                    heartbeat_timeout=GPU_HEARTBEAT_TIMEOUT,
                    retry_policy=GPU_RETRY_POLICY,
                )
            except Exception as exc:
                workflow.logger.warning(
                    "figure_aware_resummarize failed for node %s ('%s'): %s",
                    node.node_id, node.title, exc,
                )
                merge_call_record(summary, model_strong, errored=True)
                return
            node.summary = result.content.strip()
            merge_call_record(
                summary, result.model,
                started_at=result.started_at,
                ended_at=result.ended_at,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )

        if leaf_targets:
            async with asyncio.TaskGroup() as tg:
                for node in leaf_targets:
                    tg.create_task(resum_leaf(node))

        # Parents — sequential post-order so grandparents see updated parent summaries
        for node in _walk_post_order(tree.root_nodes):
            if not node.nodes:
                continue
            child_summaries = "\n\n".join(
                f"[{child.node_id}] {child.title}\n{child.summary or '(no summary)'}"
                for child in node.nodes
            )
            prompt = get_prompt(
                "summarize_from_children", prompt_style,
                title=node.title,
                child_summaries=child_summaries,
            )
            try:
                result = await workflow.execute_activity(
                    llm_text_call_activity,
                    LlmTextCallInput(config=config, model=model_fast, prompt=prompt),
                    task_queue=gpu_q,
                    start_to_close_timeout=GPU_ACTIVITY_TIMEOUT,
                    heartbeat_timeout=GPU_HEARTBEAT_TIMEOUT,
                    retry_policy=GPU_RETRY_POLICY,
                )
            except Exception as exc:
                workflow.logger.warning(
                    "summarize_from_children failed for node %s ('%s'): %s",
                    node.node_id, node.title, exc,
                )
                merge_call_record(summary, model_fast, errored=True)
                continue
            node.summary = result.content.strip()
            merge_call_record(
                summary, result.model,
                started_at=result.started_at,
                ended_at=result.ended_at,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )

    async def _finalize(
        self,
        input: ProcessPdfWorkflowInput,
        config: dict,
        tree,
        summary: dict,
        node_count: int,
        total_pages: int,
        cpu_q: str,
    ) -> ProcessPdfWorkflowOutput:
        output_path = self._tree_path(input, config)
        final_out = await workflow.execute_activity(
            finalize_activity,
            FinalizeInput(
                tree=tree,
                output_path=output_path,
                pretty_print=config.get("output", {}).get("pretty_print_json", True),
            ),
            task_queue=cpu_q,
            start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
            heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        finalize_summary(summary)
        return ProcessPdfWorkflowOutput(
            document_id=input.document_id,
            run_id=input.run_id,
            tree_path=final_out.tree_path,
            node_count=node_count,
            total_pages=total_pages,
            metrics_summary=summary,
        )

    @staticmethod
    def _tree_path(input: ProcessPdfWorkflowInput, config: dict) -> str:
        kb_root = config.get("output", {}).get("kb_root", "./kb")
        return str(Path(kb_root) / input.document_id / "tree.json")
