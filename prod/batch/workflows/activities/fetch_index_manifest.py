"""Read+validate an indexing batch manifest from S3 (or local) at workflow start.

Sibling of fetch_manifest_activity; kept separate because BatchManifest and
IndexBatchManifest are distinct pydantic types (mixing them behind a
discriminated union costs clarity for no reader-side upside).
"""

from pydantic import BaseModel, ConfigDict
from temporalio import activity

from ...artifacts import read_index_manifest
from ...models import IndexBatchManifest


class FetchIndexManifestInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    manifest_uri: str


class FetchIndexManifestOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    manifest: IndexBatchManifest


@activity.defn(name="batch_fetch-index-manifest")
async def fetch_index_manifest_activity(
    input: FetchIndexManifestInput,
) -> FetchIndexManifestOutput:
    activity.heartbeat()
    manifest = read_index_manifest(input.manifest_uri)
    return FetchIndexManifestOutput(manifest=manifest)
