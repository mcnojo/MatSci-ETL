
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


class LocalStore:
    """Disk-backed artifact store — the default for local/dev runs.

    Root is typically etl/kb/. URI scheme is ``file://``.
    """

    def __init__(self, root: str | Path):
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def read_bytes(self, uri: str) -> bytes:
        return self._path_from_uri(uri).read_bytes()

    def write_bytes(self, uri: str, data: bytes) -> None:
        path = self._path_from_uri(uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def uri_for(self, document_id: str, run_id: str, *path_parts: str) -> str:
        rel = Path(document_id, "runs", run_id, *path_parts)
        full = self._root / rel
        return full.as_uri()

    def exists(self, uri: str) -> bool:
        return self._path_from_uri(uri).exists()

    def local_path(self, uri: str) -> Path:
        """Resolve a file:// URI to an absolute Path. Convenience for local-only code."""
        return self._path_from_uri(uri)

    def _path_from_uri(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme == "file":
            return Path(unquote(parsed.path))
        if parsed.scheme == "":
            # Bare path (no scheme) — treat as relative to root
            return self._root / uri
        raise ValueError(f"LocalStore cannot handle URI scheme {parsed.scheme!r}: {uri}")
