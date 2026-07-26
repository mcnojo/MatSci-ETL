"""Local run reports: enrich metrics with cost, persist to `logs/`, pretty-print.

Naming: `logs/<utc_iso_compact>_<method>_<document_id>.json`
        e.g. `20260716T143022Z_process_paper.json`

Shape is stable — downstream tooling parses these directly. `metrics` is
the workflow's `metrics_summary` enriched in-place with `cost_usd` per
model bucket + `cost_usd_total` (null when any model was missing from
the pricing table, so unknowns can't silently zero the total).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from .pricing import estimate_cost_usd


LOG_DIR = Path("logs")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _enrich_costs(metrics_summary: dict) -> dict:
    """Attach `cost_usd` to each llm_calls bucket + a run total.

    `cost_usd_total` is None when any model in the call set is absent
    from the pricing table — the row will show '—' and we refuse to
    fabricate a total that ignores it.
    """
    total: float = 0.0
    any_unknown = False
    for model, bucket in metrics_summary.get("llm_calls", {}).items():
        cost = estimate_cost_usd(
            model,
            bucket.get("input_tokens", 0),
            bucket.get("output_tokens", 0),
        )
        bucket["cost_usd"] = cost
        if cost is None:
            any_unknown = True
        else:
            total += cost
    metrics_summary["cost_usd_total"] = None if any_unknown else round(total, 6)
    metrics_summary["cost_notes"] = (
        "one or more models absent from pipeline.pricing._PRICING; "
        "total suppressed (self-hosted models are accounted via hardware, "
        "not per-token)"
    ) if any_unknown else None
    return metrics_summary


def build_process_record(
    *,
    document_id: str,
    workflow_id: str,
    pdf_path: str,
    config_path: str,
    temporal_address: str,
    started_at_iso: str,
    ended_at_iso: str,
    tree_llm_model: str | None,
    vision_ocr_model: str | None,
    output: Any = None,   # ProcessPdfWorkflowOutput | None
    error: str | None = None,
) -> dict:
    metrics = _enrich_costs(dict(output.metrics_summary)) if output else {}
    return {
        "method": "process",
        "document_id": document_id,
        "workflow_id": workflow_id,
        "inputs": {
            "pdf_path": pdf_path,
            "config_path": config_path,
            "temporal_address": temporal_address,
            "tree_llm_model": tree_llm_model,
            "vision_ocr_model": vision_ocr_model,
        },
        "started_at": started_at_iso,
        "ended_at": ended_at_iso,
        "status": "ok" if output else "error",
        "error": error,
        "result": None if output is None else {
            "tree_path": output.tree_path,
            "node_count": output.node_count,
            "total_pages": output.total_pages,
            "index_summary": output.index_summary,
        },
        "metrics": metrics,
    }


def build_index_record(
    *,
    document_id: str,
    workflow_id: str,
    tree_uri: str,
    config_path: str,
    temporal_address: str,
    started_at_iso: str,
    ended_at_iso: str,
    embedding_model: str | None,
    output: Any = None,   # IndexDocumentWorkflowOutput | None
    error: str | None = None,
) -> dict:
    return {
        "method": "index",
        "document_id": document_id,
        "workflow_id": workflow_id,
        "inputs": {
            "tree_uri": tree_uri,
            "config_path": config_path,
            "temporal_address": temporal_address,
            "embedding_model": embedding_model,
        },
        "started_at": started_at_iso,
        "ended_at": ended_at_iso,
        "status": "ok" if output else "error",
        "error": error,
        "result": None if output is None else {
            "collection_name": output.collection_name,
            "chunk_count": output.chunk_count,
            "embedded_count": output.embedded_count,
            "indexed_count": output.indexed_count,
            "total_tokens": output.total_tokens,
        },
    }


def write_record(record: dict) -> Path:
    """Serialize record to `logs/<utc>_<method>_<document_id>.json`."""
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"{utc_stamp()}_{record['method']}_{record['document_id']}.json"
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return path


def render_process(console: Console, record: dict) -> None:
    metrics = record.get("metrics") or {}
    calls = metrics.get("llm_calls", {})
    if not calls:
        return
    table = Table(title=f"process: {record['document_id']}", show_header=True, header_style="bold")
    for col, just in [
        ("model", "left"), ("calls", "right"), ("in_tok", "right"),
        ("out_tok", "right"), ("wall_s", "right"), ("compute_s", "right"),
        ("max_conc", "right"), ("cost_usd", "right"),
    ]:
        table.add_column(col, justify=just)
    for model, b in calls.items():
        cost = b.get("cost_usd")
        table.add_row(
            model,
            str(b.get("count", 0)),
            f"{b.get('input_tokens', 0):,}",
            f"{b.get('output_tokens', 0):,}",
            f"{b.get('wall_clock_time_s', 0):.2f}",
            f"{b.get('compute_time_s', 0):.2f}",
            str(b.get("max_concurrent", 0)),
            "—" if cost is None else f"${cost:.4f}",
        )
    console.print(table)
    tot = metrics.get("token_totals", {})
    cost_total = metrics.get("cost_usd_total")
    cost_str = "unknown (see cost_notes)" if cost_total is None else f"${cost_total:.4f}"
    console.print(
        f"  tokens: in={tot.get('input', 0):,}  out={tot.get('output', 0):,}  "
        f"total={tot.get('total', 0):,}    cost: {cost_str}"
    )


def render_index(console: Console, record: dict) -> None:
    r = record.get("result") or {}
    console.print(
        f"  collection={r.get('collection_name')}  chunks={r.get('chunk_count')}  "
        f"embedded={r.get('embedded_count')}  indexed={r.get('indexed_count')}  "
        f"tokens={r.get('total_tokens')}"
    )
