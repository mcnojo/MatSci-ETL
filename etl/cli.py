"""CLI entry point — starts ProcessPdfWorkflow executions via Temporal.

Usage:
    python -m etl.cli --pdf paper.pdf
    python -m etl.cli --pdf-dir papers/ --skip-enrichment
    python -m etl.cli --pdf paper.pdf --temporal-address 10.0.1.5:7233

Temporal address resolution: --temporal-address wins; else TEMPORAL_ADDRESS
env var; else shared/temporal terraform output cpu_pipeline_public_ip:7233.
"""

import asyncio
import logging
import sys
import time
import uuid
from pathlib import Path

import click
from rich.console import Console
from prod.live.workflows.process_pdf import ProcessPdfWorkflow
from prod.live.workflows.models import ProcessPdfWorkflowInput, ProcessPdfWorkflowOutput
from shared.temporal.task_queues import LIVE_CPU_TQ, WORKFLOW_EXECUTION_TIMEOUT
from shared.config_loader import load_pipeline_config
from shared.temporal.client import connect_temporal
from shared.temporal.operator_address import resolve_operator_address

console = Console()


async def _run(
    pdf_paths: list[Path],
    config: dict,
    skip_enrichment: bool,
    temporal_address: str,
):
    client = await connect_temporal(temporal_address)
    console.print(f"Connected to Temporal at [bold]{temporal_address}[/bold]")

    t0 = time.perf_counter()
    handles = []  # list of (document_id, typed WorkflowHandle)

    for pdf_path in pdf_paths:
        document_id = pdf_path.stem
        run_id = str(uuid.uuid4())
        workflow_id = f"process-pdf-{document_id}-{run_id}"

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
        handles.append((document_id, handle))

    errors: list[str] = []
    for document_id, handle in handles:
        try:
            result: ProcessPdfWorkflowOutput = await handle.result()
            console.print(
                f"  [green]done[/green] {document_id}: "
                f"{result.node_count} nodes, {result.total_pages} pages -> {result.tree_path}"
            )
        except Exception as exc:
            errors.append(f"{document_id}: {exc}")
            console.print(f"  [red]fail[/red] {document_id}: {exc}")

    elapsed = time.perf_counter() - t0
    console.print(f"\n{len(handles)} PDFs in {elapsed:.1f}s, {len(errors)} errors")
    if errors:
        sys.exit(1)


@click.command()
@click.option("--pdf", default=None, help="Path to a single PDF.")
@click.option("--pdf-dir", default=None, help="Directory of PDFs.")
@click.option(
    "--config", "config_path",
    default="etl/config/pipeline_config.yaml",
    show_default=True,
)
@click.option("--skip-enrichment", is_flag=True, default=False)
@click.option("--temporal-address", default=None,
              help="Temporal gRPC endpoint. Resolution order: flag -> "
                   "TEMPORAL_ADDRESS env -> shared/temporal terraform output "
                   "cpu_pipeline_public_ip:7233.")
@click.option("-v", "--verbose", is_flag=True, default=False)
def main(pdf, pdf_dir, config_path, skip_enrichment, temporal_address, verbose):
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpcore", "httpx", "LiteLLM", "matplotlib", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

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

    resolved_address, source = resolve_operator_address(temporal_address)
    if source != "flag":
        console.print(f"[dim]Temporal address: {resolved_address} (from {source})[/dim]")

    asyncio.run(_run(pdfs, config, skip_enrichment, resolved_address))


if __name__ == "__main__":
    main()
