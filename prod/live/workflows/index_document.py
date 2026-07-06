"""IndexDocumentWorkflow — chunk + embed + BM25/dense-index a finalized tree.

Consumes a completed `tree.json` URI (produced by ProcessPdfWorkflow) and
publishes chunks + embeddings to OpenSearch. Deliberately decoupled from
ProcessPdfWorkflow so re-indexing (embedding-model swap, chunker tuning,
index recreation) doesn't require re-running OCR.

Stages: load_tree -> load_pages -> chunk -> embed -> index. The chunker and
embedder both re-read source artifacts through S3; nothing large crosses
Temporal history.

Task-queue routing mirrors ProcessPdfWorkflow: the CPU queue the workflow
lands on IS the motif signal, and the GPU sibling is derived from it.
"""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from pipeline.cpu_activities import (
        LoadPagesInput,
        chunk_document_activity,
        index_chunks_activity,
        load_pages_activity,
        load_tree_activity,
    )
    from pipeline.gpu_activities import embed_chunks_activity
    from shared.temporal.activity_models import (
        ChunkDocumentInput,
        ChunkDocumentOutput,
        EmbedChunksInput,
        EmbedChunksOutput,
        IndexChunksInput,
        IndexChunksOutput,
        LoadTreeInput,
        LoadTreeOutput,
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

    from .models import (
        IndexDocumentWorkflowInput,
        IndexDocumentWorkflowOutput,
    )


_GPU_SIBLING = {
    LIVE_CPU_TQ: LIVE_GPU_TQ,
    BATCH_CPU_TQ: BATCH_GPU_TQ,
}


def _resolve_index_name(input: IndexDocumentWorkflowInput) -> str:
    if input.index_name:
        return input.index_name
    try:
        return input.config["retrieval"]["opensearch"]["index_name"]
    except KeyError as e:
        raise ValueError(
            f"IndexDocumentWorkflow needs retrieval.opensearch.index_name in "
            f"config, or index_name in the workflow input; missing {e}"
        )


def _embeddings_uri(config: dict, document_id: str) -> str:
    """One canonical embeddings URI per paper — mirrors _index_artifact_uri in
    pipeline.cpu_activities. Re-runs overwrite in place; wipe_paper in
    index_chunks_activity clears the OpenSearch side.

    .npy = fp16 numpy array, shape (N, dim), positionally aligned with
    chunks.json. ~4-8× smaller than the JSON list-of-lists that preceded it.
    """
    prefix = config["output"]["assets_uri_prefix"].rstrip("/")
    return f"{prefix}/{document_id}/index/embeddings.npy"


@workflow.defn
class IndexDocumentWorkflow:
    """Chunk -> embed -> index one document.

    Idempotent per (document_id, index_name): chunk doc_ids are deterministic
    and the writer uses them as OpenSearch _id, so re-runs overwrite in place.
    """

    @workflow.run
    async def run(self, input: IndexDocumentWorkflowInput) -> IndexDocumentWorkflowOutput:
        cpu_q = workflow.info().task_queue
        gpu_q = _GPU_SIBLING.get(cpu_q, cpu_q)
        index_name = _resolve_index_name(input)

        tree_meta: LoadTreeOutput = await workflow.execute_activity(
            load_tree_activity,
            LoadTreeInput(tree_uri=input.tree_uri),
            task_queue=cpu_q,
            start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
            heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

        # Rebuild page text from the PDF referenced by the tree. Fast (PyMuPDF),
        # avoids depending on the process_pdf run's per-run scratch still existing.
        pages = await workflow.execute_activity(
            load_pages_activity,
            LoadPagesInput(
                pdf_path=tree_meta.pdf_path,
                document_id=input.document_id,
                run_id=input.run_id,
                config=input.config,
            ),
            task_queue=cpu_q,
            start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
            heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

        chunked: ChunkDocumentOutput = await workflow.execute_activity(
            chunk_document_activity,
            ChunkDocumentInput(
                document_id=input.document_id,
                run_id=input.run_id,
                tree_uri=input.tree_uri,
                pages_uri=pages.pages_uri,
                config=input.config,
            ),
            task_queue=cpu_q,
            start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
            heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

        embed_uri = _embeddings_uri(input.config, input.document_id)
        embedded: EmbedChunksOutput = await workflow.execute_activity(
            embed_chunks_activity,
            EmbedChunksInput(
                chunks_uri=chunked.chunks_uri,
                embeddings_uri_out=embed_uri,
                config=input.config,
            ),
            task_queue=gpu_q,
            start_to_close_timeout=GPU_ACTIVITY_TIMEOUT,
            heartbeat_timeout=GPU_HEARTBEAT_TIMEOUT,
            retry_policy=GPU_RETRY_POLICY,
        )

        indexed: IndexChunksOutput = await workflow.execute_activity(
            index_chunks_activity,
            IndexChunksInput(
                paper_id=tree_meta.paper_id,
                chunks_uri=chunked.chunks_uri,
                embeddings_uri=embedded.embeddings_uri,
                index_name=index_name,
                config=input.config,
            ),
            task_queue=cpu_q,
            start_to_close_timeout=CPU_ACTIVITY_TIMEOUT,
            heartbeat_timeout=CPU_HEARTBEAT_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

        return IndexDocumentWorkflowOutput(
            document_id=input.document_id,
            run_id=input.run_id,
            index_name=indexed.index_name,
            chunk_count=chunked.chunk_count,
            embedded_count=embedded.embedded_count,
            indexed_count=indexed.indexed_count,
            total_tokens=chunked.total_tokens,
        )
