"""Rich post-batch report: Temporal history + CloudWatch hardware metrics.

Wraps prod.batch.reports.build_report + write_report — the existing builder
code is unchanged; this is just an activity envelope. Runs late in the
workflow (after fan-out completes, before scale_fleet_down) so CWAgent's
~60s buffer has flushed worker metrics by the time the query window closes.

Reads TEMPORAL_ADDRESS / TEMPORAL_NAMESPACE from env (worker process).
"""

import os

from pydantic import BaseModel, ConfigDict
from temporalio import activity
from temporalio.exceptions import ApplicationError

from shared.temporal_client import connect_temporal

from ...reports import build_report, write_report


class BuildReportInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    batch_id: str
    region: str
    report_root: str
    pull_hardware: bool = True


class BuildReportOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    report_uris: dict[str, str]      # report_json_uri, report_md_uri


@activity.defn(name="batch_build-report")
async def build_report_activity(input: BuildReportInput) -> BuildReportOutput:
    activity.heartbeat()
    address = os.environ.get("TEMPORAL_ADDRESS")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    if not address:
        raise ApplicationError(
            "TEMPORAL_ADDRESS env var not set on worker — cannot walk Temporal history",
            non_retryable=True,
        )
    client = await connect_temporal(address, namespace=namespace)
    activity.heartbeat()
    report = await build_report(
        client=client, batch_id=input.batch_id, region=input.region,
        pull_hardware=input.pull_hardware,
    )
    activity.heartbeat()
    uris = write_report(report, input.report_root)
    return BuildReportOutput(report_uris=uris)
