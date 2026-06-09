"""Motif-agnostic reports: Temporal history + CloudWatch hardware metrics.

Three report shapes:
- BatchReport: bounded run over a BatchRunWorkflow + reachable children.
- LiveReport: rolling-window over standalone ProcessPdfWorkflow runs.
- ComparisonReport: side-by-side of one BatchReport vs one LiveReport.
"""

from .builder import (
    build_batch_report,
    build_comparison_report,
    build_live_report,
    write_batch_report,
    write_comparison_report,
    write_live_report,
)
from .models import (
    ActivityStats,
    BatchReport,
    ComparisonReport,
    LiveReport,
    LiveWindow,
    StatSummary,
    WorkerInstanceStats,
    WorkflowStats,
)

__all__ = [
    "ActivityStats",
    "BatchReport",
    "ComparisonReport",
    "LiveReport",
    "LiveWindow",
    "StatSummary",
    "WorkerInstanceStats",
    "WorkflowStats",
    "build_batch_report",
    "build_comparison_report",
    "build_live_report",
    "write_batch_report",
    "write_comparison_report",
    "write_live_report",
]
