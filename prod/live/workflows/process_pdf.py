"""ProcessPdfWorkflow — PDF pipeline orchestrator.

Stages: load_pages -> tree build (LLM fanout) -> extract_assets -> chandra OCR
fanout -> attach_ocr -> assign_elements -> figure-aware resummarize -> finalize.
LLM activities reference pages + config by URI staged in load_pages.
"""

import asyncio
from pathlib import Path

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from etl.pipeline.cpu_activities import (
        AssignElementsInput,
        AttachOcrInput,
        ExtractAssetsInput,
        FinalizeInput,
        LoadPagesInput,
        OcrTarget,
        OcrUpdate,
        assign_elements_activity,
        attach_ocr_activity,
        extract_assets_activity,
        finalize_activity,
        load_pages_activity,
    )
    from etl.pipeline.gpu_activities import (
        chandra_vision_call_activity,
        llm_text_call_activity,
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
        PromptSpec,
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
    from shared.schemas import TreeNode, VisualElement

    from .models import (
        ProcessPdfWorkflowInput,
        ProcessPdfWorkflowOutput,
    )


# The CPU queue the workflow lands on IS the motif signal — derive the GPU sibling.
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

        # Also stages pages + config to S3 for downstream LLM activities.
        load_out = await workflow.execute_activity(
            load_pages_activity,
            LoadPagesInput(
                pdf_path=input.pdf_path,
                document_id=input.document_id,
                run_id=input.run_id,
                config=config,
            ),
            task_queue=cpu_q,
            start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
            heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        token_counts = load_out.token_counts
        total_pages = load_out.total_pages
        pages_uri = load_out.pages_uri
        config_uri = load_out.config_uri

        if load_out.is_likely_scanned:
            workflow.logger.warning(
                "PDF appears to be scanned (low extractable text). "
                "Tree quality may be poor — consider OCR preprocessing."
            )

        # Stage 1: Build tree — workflow drives orchestration, every LLM call is a Temporal activity
        opt = build_opt_from_config(config, pages_uri)
        call_llm = self._make_call_llm(config_uri, summary, gpu_q)
        tree = await build_tree(
            token_counts, opt,
            call_llm=call_llm,
            rng=workflow.random(),
            paper_id=input.document_id,
            pdf_path=input.pdf_path,
        )
        node_count = sum(1 for _ in _flatten(tree.root_nodes))

        if input.skip_enrichment:
            return await self._finalize(input, config, tree, summary, node_count, total_pages, cpu_q)

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
        page_elements_uri = assets_out.page_elements_uri
        page_image_uris = assets_out.page_image_uris
        ocr_targets = assets_out.ocr_targets

        if config["enrichment"]["run_ocr"] and ocr_targets:
            ocr_updates = await self._enrich_ocr(ocr_targets, config, summary, gpu_q)

            # Workflow can't write to S3 (deterministic replay); merge runs as an activity.
            attach_out = await workflow.execute_activity(
                attach_ocr_activity,
                AttachOcrInput(
                    page_elements_uri=page_elements_uri,
                    ocr_updates=ocr_updates,
                    document_id=input.document_id,
                    config=config,
                ),
                task_queue=cpu_q,
                start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
                heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
            page_elements_uri = attach_out.page_elements_uri

        assign_out = await workflow.execute_activity(
            assign_elements_activity,
            AssignElementsInput(
                tree=tree,
                page_elements_uri=page_elements_uri,
                page_image_uris=page_image_uris,
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

        if config.get("enrichment", {}).get("figure_aware_resummarize", True):
            await self._resummarize(tree, config, summary, gpu_q, config_uri)

        return await self._finalize(input, config, tree, summary, node_count, total_pages, cpu_q)

    def _make_call_llm(self, config_uri: str, summary: dict, gpu_q: str):
        async def call(model, spec: PromptSpec, *, json_mode=False, temperature=0.0) -> LlmResult:
            result = await workflow.execute_activity(
                llm_text_call_activity,
                LlmTextCallInput(
                    prompt_spec=spec, config_uri=config_uri, model=model,
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
        self, targets: list[OcrTarget], config: dict, summary: dict, gpu_q: str,
    ) -> list[OcrUpdate]:
        vision_cfg = config["vision_server"]
        ocr_model_label = f"ocr:{vision_cfg['ocr_model']}"
        updates: list[OcrUpdate] = []

        async def call_one(target: OcrTarget):
            try:
                result = await workflow.execute_activity(
                    chandra_vision_call_activity,
                    ChandraCallInput(
                        base_url=vision_cfg["base_url"],
                        api_key=vision_cfg["api_key"],
                        model=vision_cfg["ocr_model"],
                        image_uri=target.asset_uri,
                        prompt=CHANDRA_OCR_LAYOUT_PROMPT,
                    ),
                    task_queue=gpu_q,
                    start_to_close_timeout=GPU_ACTIVITY_TIMEOUT,
                    heartbeat_timeout=GPU_HEARTBEAT_TIMEOUT,
                    retry_policy=GPU_RETRY_POLICY,
                )
            except Exception as exc:
                updates.append(OcrUpdate(
                    element_id=target.element_id,
                    page_idx=target.page_idx,
                    elem_idx=target.elem_idx,
                    ocr_text=f"ERROR: {exc}",
                ))
                merge_call_record(summary, ocr_model_label, errored=True)
                return
            updates.append(OcrUpdate(
                element_id=target.element_id,
                page_idx=target.page_idx,
                elem_idx=target.elem_idx,
                ocr_text=result.content,
                ocr_parsed=parse_chandra(result.content),
            ))
            merge_call_record(
                summary, ocr_model_label,
                started_at=result.started_at,
                ended_at=result.ended_at,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )

        async with asyncio.TaskGroup() as tg:
            for target in targets:
                tg.create_task(call_one(target))

        return updates

    async def _resummarize(
        self, tree, config: dict, summary: dict, gpu_q: str, config_uri: str,
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
            spec = PromptSpec(
                name="figure_aware_resummarize", style=prompt_style,
                small_kwargs={
                    "title": node.title,
                    "prior_summary": node.summary or "",
                    "figure_block": figure_block,
                },
            )
            try:
                result = await workflow.execute_activity(
                    llm_text_call_activity,
                    LlmTextCallInput(
                        prompt_spec=spec, config_uri=config_uri, model=model_strong,
                    ),
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
            spec = PromptSpec(
                name="summarize_from_children", style=prompt_style,
                small_kwargs={"title": node.title, "child_summaries": child_summaries},
            )
            try:
                result = await workflow.execute_activity(
                    llm_text_call_activity,
                    LlmTextCallInput(
                        prompt_spec=spec, config_uri=config_uri, model=model_fast,
                    ),
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
