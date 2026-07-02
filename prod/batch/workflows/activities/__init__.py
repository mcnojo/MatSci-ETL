"""Per-stage activities for the batch path.

The `activities` list is what Worker(..., activities=...) consumes — keep it
in lock-step with the @activity.defn defs across this folder.
"""

from .await_pollers import await_pollers_activity
from .build_report import build_report_activity
from .fetch_index_manifest import fetch_index_manifest_activity
from .fetch_manifest import fetch_manifest_activity
from .scale_fleet import scale_fleet_down_activity, scale_fleet_up_activity
from .write_report import write_report_activity

activities = [
    fetch_manifest_activity,
    fetch_index_manifest_activity,
    write_report_activity,
    scale_fleet_up_activity,
    scale_fleet_down_activity,
    await_pollers_activity,
    build_report_activity,
]

__all__ = [
    "activities",
    "await_pollers_activity",
    "build_report_activity",
    "fetch_index_manifest_activity",
    "fetch_manifest_activity",
    "scale_fleet_down_activity",
    "scale_fleet_up_activity",
    "write_report_activity",
]
