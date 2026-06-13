"""ASG capacity activities — drive set_desired_capacity on the two batch ASGs.

scale_fleet_up_activity is non-retryable on quota errors (LimitExceeded /
ServiceLinkedRoleFailure / etc.): retrying the same call won't get past the
quota, so the workflow should fail fast and surface the issue to the operator.

scale_fleet_down_activity is best-effort across both ASGs — it always attempts
both, returning per-ASG outcomes so a partial failure is visible without
preventing the other half from succeeding.
"""

import asyncio

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict
from temporalio import activity
from temporalio.exceptions import ApplicationError

# AWS error codes that mean "no amount of retry will help."
_QUOTA_ERRORS = frozenset({
    "LimitExceeded",
    "LimitExceededException",
    "ResourceLimitExceeded",
    "ValidationError",            # bad ASG name, malformed desired count
    "ValidationException",
})


class ScaleFleetUpInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    region: str
    cpu_queue_asg_name: str
    cpu_queue_desired: int
    gpu_queue_asg_name: str
    gpu_queue_desired: int


class ScaleFleetUpOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    cpu_queue_desired: int
    gpu_queue_desired: int


class ScaleFleetDownInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    region: str
    cpu_queue_asg_name: str
    gpu_queue_asg_name: str


class ScaleFleetDownOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    cpu_ok: bool
    gpu_ok: bool
    cpu_error: str | None = None
    gpu_error: str | None = None


def _set_desired(region: str, asg_name: str, desired: int) -> None:
    boto3.client("autoscaling", region_name=region).set_desired_capacity(
        AutoScalingGroupName=asg_name, DesiredCapacity=desired,
    )


def _live_instance_count(region: str, asg_names: list[str]) -> dict[str, int]:
    """Per-ASG count of attached instances in ANY lifecycle state.

    Includes Terminating / Terminating:Wait / Terminating:Proceed — only when
    AWS finishes the termination and removes the ENI does the instance
    disappear from the ASG's instance list. That removal is the signal we
    actually want: until it happens, a subsequent SetDesiredCapacity > 0
    can rescue the instance from termination and re-issue work to the same
    box (with whatever stale code it cloned at boot).
    """
    resp = boto3.client("autoscaling", region_name=region).describe_auto_scaling_groups(
        AutoScalingGroupNames=asg_names,
    )
    return {asg["AutoScalingGroupName"]: len(asg["Instances"]) for asg in resp["AutoScalingGroups"]}


async def _wait_for_full_drain(
    region: str, asg_names: list[str], poll_s: float = 10.0,
) -> None:
    """Block until every named ASG has zero attached instances.

    Heartbeats Temporal each iteration so a slow drain doesn't trip the
    heartbeat timeout. Total wall-clock is bounded by the activity's
    start_to_close timeout — no internal cap (the activity timeout is the
    single source of truth).
    """
    if not asg_names:
        return
    while True:
        activity.heartbeat()
        counts = await asyncio.to_thread(_live_instance_count, region, asg_names)
        if all(n == 0 for n in counts.values()):
            return
        await asyncio.sleep(poll_s)


@activity.defn(name="batch_scale-fleet-up")
async def scale_fleet_up_activity(input: ScaleFleetUpInput) -> ScaleFleetUpOutput:
    """Scale both batch ASGs to their target counts.

    Quota / validation errors are surfaced as non-retryable: the workflow
    fails fast and runs its finally-block teardown.
    """
    activity.heartbeat()
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(asyncio.to_thread(
                _set_desired, input.region, input.cpu_queue_asg_name, input.cpu_queue_desired,
            ))
            tg.create_task(asyncio.to_thread(
                _set_desired, input.region, input.gpu_queue_asg_name, input.gpu_queue_desired,
            ))
    except* ClientError as eg:
        codes = [e.response.get("Error", {}).get("Code", "") for e in eg.exceptions]
        msg = "; ".join(str(e) for e in eg.exceptions)
        non_retryable = any(c in _QUOTA_ERRORS for c in codes)
        raise ApplicationError(
            f"scale_fleet_up failed: {msg}",
            non_retryable=non_retryable,
        ) from eg

    return ScaleFleetUpOutput(
        cpu_queue_desired=input.cpu_queue_desired,
        gpu_queue_desired=input.gpu_queue_desired,
    )


@activity.defn(name="batch_scale-fleet-down")
async def scale_fleet_down_activity(input: ScaleFleetDownInput) -> ScaleFleetDownOutput:
    """Scale both batch ASGs to zero AND block until AWS has actually removed
    every instance from the ASG.

    Why the drain wait: SetDesiredCapacity returns the instant AWS accepts the
    request, not when instances finish terminating. If the next BatchRunWorkflow
    starts during that window, its scale_fleet_up sees existing capacity and
    rescues the about-to-die instances — keeping them on the OLD code they
    cloned at boot. Waiting for the ASG's instance list to empty closes the
    race: post-wait, the next scale-up is guaranteed to launch fresh boxes
    that re-clone the repo.

    Per-ASG isolated for the SetDesiredCapacity call so one failure doesn't
    prevent the other from succeeding. The drain wait only covers ASGs whose
    request was accepted — no point polling on one that errored.
    """
    activity.heartbeat()
    cpu_err: str | None = None
    gpu_err: str | None = None
    try:
        await asyncio.to_thread(_set_desired, input.region, input.cpu_queue_asg_name, 0)
    except Exception as e:
        cpu_err = str(e)
    try:
        await asyncio.to_thread(_set_desired, input.region, input.gpu_queue_asg_name, 0)
    except Exception as e:
        gpu_err = str(e)

    asgs_to_drain = [
        name for name, err in (
            (input.cpu_queue_asg_name, cpu_err),
            (input.gpu_queue_asg_name, gpu_err),
        ) if err is None
    ]
    await _wait_for_full_drain(input.region, asgs_to_drain)

    return ScaleFleetDownOutput(
        cpu_ok=cpu_err is None, gpu_ok=gpu_err is None,
        cpu_error=cpu_err, gpu_error=gpu_err,
    )
