"""Shared activity heartbeat helper.

Lives outside cpu/gpu activity modules so both can import it without one
pulling the other's deps (the GPU lane must not see torch/cv2 via accidental
co-residency).
"""

import asyncio
from typing import TypeVar

from temporalio import activity

_T = TypeVar("_T")


async def await_with_heartbeats(coro, *, interval_s: float = 20.0) -> _T:
    """Await `coro` while emitting Temporal heartbeats every `interval_s`.

    Shield the underlying task from `wait_for`'s periodic-timeout cancellation
    so the network call keeps running; we wake just long enough to heartbeat.
    Propagate cancellation of the activity itself to the inner task — shield
    alone would swallow it.
    """
    task = asyncio.ensure_future(coro)
    try:
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=interval_s)
            except asyncio.TimeoutError:
                activity.heartbeat()
    except BaseException:
        task.cancel()
        raise
