
from typing import Protocol, runtime_checkable


@runtime_checkable
class ArtifactStore(Protocol):
    """Storage backend for pipeline artifacts.

    Implementations: LocalStore (disk), S3Store (boto3).
    Activities pass URIs between stages — anything larger than a few KB
    goes through the store. Keys are deterministic per (document_id, run_id, stage)
    so retries overwrite the same object.
    """

    def read_bytes(self, uri: str) -> bytes:
        """Read raw bytes from a storage URI."""
        ...

    def write_bytes(self, uri: str, data: bytes) -> None:
        """Write raw bytes to a storage URI."""
        ...

    def uri_for(self, document_id: str, run_id: str, *path_parts: str) -> str:
        """Build a deterministic URI for a given artifact path.

        Example: store.uri_for("hybrid", "run-001", "tree", "tree.json")
        Local:   file:///abs/path/kb/hybrid/runs/run-001/tree/tree.json
        S3:      s3://bucket/documents/hybrid/runs/run-001/tree/tree.json
        """
        ...

    def exists(self, uri: str) -> bool:
        """Check whether an artifact exists at the given URI."""
        ...


def create_store(config: dict) -> ArtifactStore:
    """Instantiate the right ArtifactStore from the pipeline config.

    Reads config["storage"]["backend"] ("local" | "s3").
    Falls back to LocalStore with config["output"]["kb_root"] if no
    storage section exists (backwards compat with pre-storage configs).
    """
    storage_cfg = config.get("storage", {})
    backend = storage_cfg.get("backend", "local")

    if backend == "s3":
        from .s3 import S3Store

        s3_cfg = storage_cfg.get("s3", {})
        return S3Store(
            bucket=s3_cfg["bucket"],
            prefix=s3_cfg.get("prefix", ""),
            region_name=s3_cfg.get("region"),
        )

    from .local import LocalStore

    local_cfg = storage_cfg.get("local", {})
    root = local_cfg.get("root") or config.get("output", {}).get("kb_root", "./kb")
    return LocalStore(root)
