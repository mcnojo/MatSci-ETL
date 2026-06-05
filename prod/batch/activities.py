"""Temporal activities for the batch path.

Stage-level activities run on the cpu-task-queue alongside the live ETL
activities. The worker process registers both this `activities` list and the
one from `etl.pipeline.activities`.

  fetch_manifest_activity:  read s3://.../manifest.json -> BatchManifest
  write_report_activity:    write summary.json + per_item.csv + failures.jsonl
"""

from pydantic import BaseModel, ConfigDict
from temporalio import activity

from .artifacts import read_manifest, write_report_files
from .models import BatchManifest


class FetchManifestInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    manifest_uri: str


class FetchManifestOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    manifest: BatchManifest


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


@activity.defn(name="batch_fetch-manifest")
async def fetch_manifest_activity(input: FetchManifestInput) -> FetchManifestOutput:
    activity.heartbeat()
    manifest = read_manifest(input.manifest_uri)
    return FetchManifestOutput(manifest=manifest)


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


activities = [fetch_manifest_activity, write_report_activity]
