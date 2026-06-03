"""Temporal client helper with the Pydantic data converter wired in.

All call sites in this codebase must use this helper instead of
`Client.connect` directly, so that Pydantic workflow/activity I/O
round-trips as typed models on both worker and client sides.
"""

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter


async def connect_temporal(address: str, namespace: str = "default") -> Client:
    return await Client.connect(
        address,
        namespace=namespace,
        data_converter=pydantic_data_converter,
    )
