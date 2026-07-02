"""Activity (and shared state) used by test_heartbeat_cancel.

Kept in a separate module from the workflow so the Temporal workflow sandbox
doesn't have to (re-)import openai / boto3 / pymupdf at validation time.
"""

import asyncio

from pydantic import BaseModel, ConfigDict
from temporalio import activity

from pipeline.heartbeat import await_with_heartbeats


# Module-level flags the test inspects after the activity is cancelled
inner_cancelled_event = asyncio.Event()
inner_finally_ran_event = asyncio.Event()


class HangInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    sleep_for_s: float = 30.0


@activity.defn(name="hang-with-heartbeats")
async def hang_with_heartbeats(input: HangInput) -> str:
    """Long-running activity that drives await_with_heartbeats.

    The inner coroutine sleeps for `sleep_for_s` seconds. On cancellation it
    sets `inner_cancelled_event` (proving asyncio cancellation propagated
    through the shield) and `inner_finally_ran_event` (proving the finally
    block executed, i.e. cleanup ran rather than being orphaned).
    """
    activity.heartbeat()

    async def inner() -> str:
        try:
            await asyncio.sleep(input.sleep_for_s)
            return "completed-without-cancel"
        except asyncio.CancelledError:
            inner_cancelled_event.set()
            raise
        finally:
            inner_finally_ran_event.set()

    return await await_with_heartbeats(inner(), interval_s=2.0)


def reset_flags() -> None:
    """Clear the module-level events so the test can run repeatedly in one process."""
    inner_cancelled_event.clear()
    inner_finally_ran_event.clear()
