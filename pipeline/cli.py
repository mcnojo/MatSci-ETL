"""CLI entry point — starts pipeline workflows via Temporal.

Subcommands:
    process   — ProcessPdfWorkflow: PDF -> DocumentTree (existing route)
    index     — IndexDocumentWorkflow: tree -> chunks -> BM25 + dense vectors

Usage:
    python -m pipeline.cli process --pdf paper.pdf
    python -m pipeline.cli process --pdf-dir papers/ --skip-enrichment
    python -m pipeline.cli index --tree-uri s3://bucket/trees/foo/tree.json
    python -m pipeline.cli index --tree-dir trees/ --index-name chem-lit-v1
    python -m pipeline.cli process --pdf paper.pdf --temporal-address 10.0.1.5:7233

Temporal address resolution (any subcommand): --temporal-address wins; else
TEMPORAL_ADDRESS env; else shared/temporal terraform output.
"""

import asyncio
import json
import logging
import sys
import time
import uuid
from pathlib import Path

import click
from rich.console import Console

from prod.live.workflows.index_document import IndexDocumentWorkflow
from prod.live.workflows.models import (
    IndexDocumentWorkflowInput,
    IndexDocumentWorkflowOutput,
    ProcessPdfWorkflowInput,
    ProcessPdfWorkflowOutput,
)
from prod.live.workflows.process_pdf import ProcessPdfWorkflow
from shared.config_loader import load_pipeline_config
from shared.temporal.client import connect_temporal
from shared.temporal.operator_address import resolve_operator_address
from shared.temporal.task_queues import LIVE_CPU_TQ, WORKFLOW_EXECUTION_TIMEOUT

from .run_report import (
    build_index_record,
    build_process_record,
    render_index,
    render_process,
    utc_iso,
    write_record,
)

console = Console()
_DEFAULT_CONFIG = "pipeline/config/pipeline_config.yaml"


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpcore", "httpx", "LiteLLM", "matplotlib", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _resolve_temporal_address(flag_value: str | None) -> str:
    address, source = resolve_operator_address(flag_value)
    if source != "flag":
        console.print(f"[dim]Temporal address: {address} (from {source})[/dim]")
    return address


# process subcommand — PDF -> DocumentTree

async def _run_process(
    pdf_paths: list[Path],
    config: dict,
    config_path: str,
    skip_enrichment: bool,
    temporal_address: str,
) -> None:
    client = await connect_temporal(temporal_address)
    console.print(f"Connected to Temporal at [bold]{temporal_address}[/bold]")

    t0 = time.perf_counter()
    tree_llm_model = (config.get("tree_llm") or {}).get("model")
    vision_ocr_model = (config.get("vision_server") or {}).get("ocr_model")
    handles = []

    for pdf_path in pdf_paths:
        document_id = pdf_path.stem
        run_id = str(uuid.uuid4())
        workflow_id = f"process-pdf-{document_id}-{run_id}"

        started_iso = utc_iso()
        handle = await client.start_workflow(
            ProcessPdfWorkflow.run,
            ProcessPdfWorkflowInput(
                document_id=document_id,
                run_id=run_id,
                pdf_path=str(pdf_path),
                config=config,
                skip_enrichment=skip_enrichment,
            ),
            id=workflow_id,
            task_queue=LIVE_CPU_TQ,
            execution_timeout=WORKFLOW_EXECUTION_TIMEOUT,
        )
        console.print(f"  Started [cyan]{document_id}[/cyan] -> {workflow_id}")
        handles.append((document_id, workflow_id, str(pdf_path), started_iso, handle))

    errors: list[str] = []
    for document_id, workflow_id, pdf_path_str, started_iso, handle in handles:
        result: ProcessPdfWorkflowOutput | None = None
        error: str | None = None
        try:
            result = await handle.result()
            console.print(
                f"  [green]done[/green] {document_id}: "
                f"{result.node_count} nodes, {result.total_pages} pages -> {result.tree_path}"
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            errors.append(f"{document_id}: {exc}")
            console.print(f"  [red]fail[/red] {document_id}: {exc}")

        record = build_process_record(
            document_id=document_id,
            workflow_id=workflow_id,
            pdf_path=pdf_path_str,
            config_path=config_path,
            temporal_address=temporal_address,
            started_at_iso=started_iso,
            ended_at_iso=utc_iso(),
            tree_llm_model=tree_llm_model,
            vision_ocr_model=vision_ocr_model,
            output=result,
            error=error,
        )
        log_path = write_record(record)
        if result is not None:
            render_process(console, record)
        console.print(f"  [dim]log: {log_path}[/dim]")

    elapsed = time.perf_counter() - t0
    console.print(f"\n{len(handles)} PDFs in {elapsed:.1f}s, {len(errors)} errors")
    if errors:
        sys.exit(1)


# index subcommand — tree -> BM25 + dense index

def _derive_document_id(tree_uri: str) -> str:
    """Trees live at .../<document_id>/tree.json; the parent directory is the
    document_id. For s3:// URIs, split on '/' after stripping trailing tree.json.
    """
    stem = tree_uri.rstrip("/")
    if stem.endswith("/tree.json"):
        stem = stem[: -len("/tree.json")]
    return stem.rsplit("/", 1)[-1]


async def _run_index(
    tree_uris: list[str],
    config: dict,
    config_path: str,
    index_name: str | None,
    temporal_address: str,
) -> None:
    client = await connect_temporal(temporal_address)
    console.print(f"Connected to Temporal at [bold]{temporal_address}[/bold]")

    t0 = time.perf_counter()
    embedding_model = (config.get("embedding_server") or {}).get("model")
    handles: list[tuple[str, str, str, str, object]] = []

    for tree_uri in tree_uris:
        document_id = _derive_document_id(tree_uri)
        run_id = str(uuid.uuid4())
        workflow_id = f"index-document-{document_id}-{run_id}"

        started_iso = utc_iso()
        handle = await client.start_workflow(
            IndexDocumentWorkflow.run,
            IndexDocumentWorkflowInput(
                document_id=document_id,
                run_id=run_id,
                tree_uri=tree_uri,
                config=config,
                index_name=index_name,
            ),
            id=workflow_id,
            task_queue=LIVE_CPU_TQ,
            execution_timeout=WORKFLOW_EXECUTION_TIMEOUT,
        )
        console.print(f"  Started [cyan]{document_id}[/cyan] -> {workflow_id}")
        handles.append((document_id, workflow_id, tree_uri, started_iso, handle))

    errors: list[str] = []
    for document_id, workflow_id, tree_uri, started_iso, handle in handles:
        result: IndexDocumentWorkflowOutput | None = None
        error: str | None = None
        try:
            result = await handle.result()
            console.print(
                f"  [green]done[/green] {document_id}: {result.chunk_count} chunks "
                f"({result.total_tokens} tokens) -> {result.index_name} "
                f"[{result.indexed_count} indexed]"
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            errors.append(f"{document_id}: {exc}")
            console.print(f"  [red]fail[/red] {document_id}: {exc}")

        record = build_index_record(
            document_id=document_id,
            workflow_id=workflow_id,
            tree_uri=tree_uri,
            config_path=config_path,
            temporal_address=temporal_address,
            started_at_iso=started_iso,
            ended_at_iso=utc_iso(),
            embedding_model=embedding_model,
            output=result,
            error=error,
        )
        log_path = write_record(record)
        if result is not None:
            render_index(console, record)
        console.print(f"  [dim]log: {log_path}[/dim]")

    elapsed = time.perf_counter() - t0
    console.print(f"\n{len(handles)} documents in {elapsed:.1f}s, {len(errors)} errors")
    if errors:
        sys.exit(1)


# Click surface

@click.group()
def cli() -> None:
    """Pipeline workflow dispatcher."""


@cli.command("process")
@click.option("--pdf", default=None, help="Path to a single PDF.")
@click.option("--pdf-dir", default=None, help="Directory of PDFs.")
@click.option("--config", "config_path", default=_DEFAULT_CONFIG, show_default=True)
@click.option("--skip-enrichment", is_flag=True, default=False)
@click.option("--temporal-address", default=None,
              help="Temporal gRPC endpoint. Falls back to TEMPORAL_ADDRESS env "
                   "then to shared/temporal terraform output.")
@click.option("-v", "--verbose", is_flag=True, default=False)
def process_cmd(pdf, pdf_dir, config_path, skip_enrichment, temporal_address, verbose):
    """Run ProcessPdfWorkflow: PDF -> DocumentTree."""
    _setup_logging(verbose)
    config = load_pipeline_config(config_path)

    if pdf and pdf_dir:
        console.print("[red]Provide --pdf or --pdf-dir, not both.[/red]")
        sys.exit(1)

    if pdf:
        pdfs = [Path(pdf).resolve()]
    elif pdf_dir:
        pdfs = sorted(p.resolve() for p in Path(pdf_dir).glob("*.pdf"))
        console.print(f"Found {len(pdfs)} PDFs in {pdf_dir}")
    else:
        console.print("[red]Provide --pdf or --pdf-dir.[/red]")
        sys.exit(1)

    if not pdfs:
        console.print("[red]No PDFs found.[/red]")
        sys.exit(1)

    address = _resolve_temporal_address(temporal_address)
    asyncio.run(_run_process(pdfs, config, config_path, skip_enrichment, address))


@cli.command("index")
@click.option("--tree-uri", default=None,
              help="Single tree.json URI (local path or s3://).")
@click.option("--tree-dir", default=None,
              help="Directory of tree.json files; recurses one level (looks for "
                   "<child>/tree.json).")
@click.option("--manifest", "manifest_path", default=None,
              help='JSON file: [{"document_id": "...", "tree_uri": "..."}]. '
                   'Alternative to --tree-uri/--tree-dir when trees are in '
                   'non-conventional locations.')
@click.option("--index-name", default=None,
              help="OpenSearch index name; falls back to "
                   "retrieval.opensearch.index_name in config.")
@click.option("--config", "config_path", default=_DEFAULT_CONFIG, show_default=True)
@click.option("--temporal-address", default=None,
              help="Temporal gRPC endpoint. Falls back to TEMPORAL_ADDRESS env "
                   "then to shared/temporal terraform output.")
@click.option("-v", "--verbose", is_flag=True, default=False)
def index_cmd(tree_uri, tree_dir, manifest_path, index_name, config_path,
              temporal_address, verbose):
    """Run IndexDocumentWorkflow: tree -> chunks -> BM25 + dense index."""
    _setup_logging(verbose)
    config = load_pipeline_config(config_path)

    sources = [x for x in (tree_uri, tree_dir, manifest_path) if x]
    if len(sources) != 1:
        console.print("[red]Provide exactly one of --tree-uri / --tree-dir / --manifest.[/red]")
        sys.exit(1)

    if tree_uri:
        uris = [tree_uri]
    elif tree_dir:
        d = Path(tree_dir).resolve()
        uris = sorted(str(p) for p in d.glob("*/tree.json"))
        console.print(f"Found {len(uris)} tree.json files under {tree_dir}")
    else:
        entries = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        uris = [e["tree_uri"] for e in entries]

    if not uris:
        console.print("[red]No trees found.[/red]")
        sys.exit(1)

    address = _resolve_temporal_address(temporal_address)
    asyncio.run(_run_index(uris, config, config_path, index_name, address))


if __name__ == "__main__":
    cli()
