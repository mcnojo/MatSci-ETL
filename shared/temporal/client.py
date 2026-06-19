"""Temporal client helper with the Pydantic data converter wired in.

All call sites use this instead of `Client.connect` directly so Pydantic
workflow/activity I/O round-trips as typed models on both ends.
"""

from dataclasses import replace

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import PayloadLimitsConfig


# Match server's 16 MB blob ceiling (shared/temporal/dynamicconfig.yaml); warn
# at half so growth surfaces in logs before it stalls a workflow.
_pydantic_converter = replace(
    pydantic_data_converter,
    payload_limits=PayloadLimitsConfig(
        memo_size_warning=2048,
        payload_size_warning=8 * 1024 * 1024,
    ),
)


async def connect_temporal(address: str, namespace: str = "default") -> Client:
    return await Client.connect(
        address,
        namespace=namespace,
        data_converter=_pydantic_converter,
    )
