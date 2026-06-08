"""Per-item CSV / failures JSONL / summary JSON writer.

The richer JSON+Markdown report (built off Temporal history + CloudWatch
metrics) is the build_report_activity's job. This activity handles the
ItemResult-derived flat outputs that operators grep through.
"""

from pydantic import BaseModel, ConfigDict
from temporalio import activity

from ...artifacts import write_report_files


class WriteReportInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    report_root: str
    batch_id: str
    summary: dict
    per_item: list[dict]
    failures: list[dict]


class WriteReportOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    report_uris: dict[str, str]


@activity.defn(name="batch_write-report")
async def write_report_activity(input: WriteReportInput) -> WriteReportOutput:
    activity.heartbeat()
    uris = write_report_files(
        report_root=input.report_root,
        batch_id=input.batch_id,
        summary=input.summary,
        per_item=input.per_item,
        failures=input.failures,
    )
    return WriteReportOutput(report_uris=uris)
