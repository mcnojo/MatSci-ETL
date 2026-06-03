"""Single entry point for loading the pipeline YAML.

Both the interactive CLI (`etl.cli`) and the SQS-driven ingestion consumer
(`prod.ingestion.consumer`) submit the same pipeline config to Temporal.
Relative paths inside that config (e.g. `output.kb_root: "./kb"`) are
**anchored to the config file's directory** here, before serialization —
so the workflow's worker process sees absolute paths regardless of which
directory it was launched from.

Without this anchoring, the worker's CWD silently picks the artifact root,
which depends on how the user invoked `python -m prod.worker` and is a
recipe for KB output landing in surprising places.
"""

from pathlib import Path

import yaml

from .vllm_resolve import resolve_config_urls


# Path-typed fields in the pipeline config that should be resolved
# against the config file's directory if they are relative.
# Format: tuple of (section, key) — both must exist for resolution to apply.
_RELATIVE_PATH_FIELDS: tuple[tuple[str, str], ...] = (
    ("output", "kb_root"),
    ("storage", "local", "root"),  # scaffolding for the future S3 cutover
)


def _anchor_relative_paths(cfg: dict, config_dir: Path) -> None:
    """Resolve relative path fields in `cfg` against `config_dir`. In-place."""
    for path_spec in _RELATIVE_PATH_FIELDS:
        *parents, leaf = path_spec
        node = cfg
        for parent in parents:
            node = node.get(parent) if isinstance(node, dict) else None
            if node is None:
                break
        if not isinstance(node, dict):
            continue
        raw = node.get(leaf)
        if not isinstance(raw, str):
            continue
        p = Path(raw)
        if not p.is_absolute():
            node[leaf] = str((config_dir / p).resolve())


def load_pipeline_config(config_path: str | Path) -> dict:
    """Load a pipeline YAML, anchor relative paths, resolve vLLM URLs."""
    config_path = Path(config_path).resolve()
    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}
    config_dir = config_path.parent
    cfg["_config_dir"] = str(config_dir)
    _anchor_relative_paths(cfg, config_dir)
    resolve_config_urls(cfg)
    return cfg
