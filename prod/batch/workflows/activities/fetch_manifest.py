"""Read+validate a batch manifest from S3 (or local) at workflow start."""

from pydantic import BaseModel, ConfigDict
from temporalio import activity

from ...artifacts import read_manifest
from ...models import BatchManifest


class FetchManifestInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    manifest_uri: str


class FetchManifestOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    manifest: BatchManifest


@activity.defn(name="batch_fetch-manifest")
async def fetch_manifest_activity(input: FetchManifestInput) -> FetchManifestOutput:
    activity.heartbeat()
    manifest = read_manifest(input.manifest_uri)
    return FetchManifestOutput(manifest=manifest)
