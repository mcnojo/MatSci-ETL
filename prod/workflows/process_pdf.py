"""ProcessPdfWorkflow — orchestrates the PDF pipeline via Temporal.

Routes activities to CPU/GPU task queues with per-stage retry policies.
"""

from pathlib import Path

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from etl.pipeline.activities import (
        AssignElementsInput,
        BuildTreeInput,
        EnrichOcrInput,
        ExtractAssetsInput,
        FinalizeInput,
        ResummarizeInput,
        build_tree_activity,
        extract_assets_activity,
        enrich_ocr_activity,
        assign_elements_activity,
        resummarize_activity,
        finalize_activity,
    )
    from prod.task_queues import (
        CPU_TASK_QUEUE,
        GPU_TASK_QUEUE,
        CPU_ACTIVITY_TIMEOUT,
        GPU_ACTIVITY_TIMEOUT,
        DEFAULT_RETRY_POLICY,
        GPU_RETRY_POLICY,
    )
    from .models import ProcessPdfWorkflowInput, ProcessPdfWorkflowOutput


def _flatten(nodes):
    for n in nodes:
        yield n
        yield from _flatten(n.nodes)


@workflow.defn
class ProcessPdfWorkflow:
    """Durable orchestrator for the PDF -> document tree pipeline.

    Each stage is an independently retryable Temporal activity.
    Activities are idempotent: deterministic artifact keys keyed on
    (document_id, run_id) mean retries overwrite the same objects.
    """

    @workflow.run
    async def run(self, input: ProcessPdfWorkflowInput) -> ProcessPdfWorkflowOutput:
        config = input.config

        # Stage 1: Build structural tree (GPU — uses text LLM)
        build_out = await workflow.execute_activity(
            build_tree_activity,
            BuildTreeInput(
                pdf_path=input.pdf_path,
                document_id=input.document_id,
                run_id=input.run_id,
                config=config,
            ),
            task_queue=GPU_TASK_QUEUE,
            start_to_close_timeout=GPU_ACTIVITY_TIMEOUT,
            retry_policy=GPU_RETRY_POLICY,
        )
        tree = build_out.tree

        if input.skip_enrichment:
            output_path = self._tree_path(input, config)
            final_out = await workflow.execute_activity(
                finalize_activity,
                FinalizeInput(
                    tree=tree,
                    output_path=output_path,
                    pretty_print=config.get("output", {}).get("pretty_print_json", True),
                ),
                task_queue=CPU_TASK_QUEUE,
                start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
            return ProcessPdfWorkflowOutput(
                document_id=input.document_id,
                run_id=input.run_id,
                tree_path=final_out.tree_path,
                node_count=build_out.node_count,
                total_pages=build_out.total_pages,
            )

        # Stage 2: Extract visual assets (CPU — PyMuPDF + layout detection)
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
            task_queue=CPU_TASK_QUEUE,
            start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

        # Stage 3: Enrich with OCR (GPU — chandra calls)
        ocr_out = await workflow.execute_activity(
            enrich_ocr_activity,
            EnrichOcrInput(
                page_elements=assets_out.page_elements,
                config=config,
            ),
            task_queue=GPU_TASK_QUEUE,
            start_to_close_timeout=GPU_ACTIVITY_TIMEOUT,
            retry_policy=GPU_RETRY_POLICY,
        )

        # Stage 4: Assign elements to tree + chem extraction (CPU)
        assign_out = await workflow.execute_activity(
            assign_elements_activity,
            AssignElementsInput(
                tree=tree,
                page_elements=ocr_out.page_elements,
                pdf_path=input.pdf_path,
                document_id=input.document_id,
                config=config,
            ),
            task_queue=CPU_TASK_QUEUE,
            start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        tree = assign_out.tree

        # Stage 5: Figure-aware re-summarization (GPU — text LLM)
        if config.get("enrichment", {}).get("figure_aware_resummarize", True):
            resum_out = await workflow.execute_activity(
                resummarize_activity,
                ResummarizeInput(tree=tree, config=config),
                task_queue=GPU_TASK_QUEUE,
                start_to_close_timeout=GPU_ACTIVITY_TIMEOUT,
                retry_policy=GPU_RETRY_POLICY,
            )
            tree = resum_out.tree

        # Stage 6: Finalize (CPU — serialize tree to JSON)
        output_path = self._tree_path(input, config)
        final_out = await workflow.execute_activity(
            finalize_activity,
            FinalizeInput(
                tree=tree,
                output_path=output_path,
                pretty_print=config.get("output", {}).get("pretty_print_json", True),
            ),
            task_queue=CPU_TASK_QUEUE,
            start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

        return ProcessPdfWorkflowOutput(
            document_id=input.document_id,
            run_id=input.run_id,
            tree_path=final_out.tree_path,
            node_count=build_out.node_count,
            total_pages=build_out.total_pages,
        )

    @staticmethod
    def _tree_path(input: ProcessPdfWorkflowInput, config: dict) -> str:
        kb_root = config.get("output", {}).get("kb_root", "./kb")
        return str(Path(kb_root) / input.document_id / "tree.json")
