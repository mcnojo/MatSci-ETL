"""Single entry point for loading the pipeline YAML.

Both the interactive CLI (`pipeline.cli`) and the SQS-driven ingestion consumer
(`prod.live.ingestion.consumer`) submit the same pipeline config to Temporal.
Relative paths inside that config (e.g. `output.kb_root: "./kb"`) are
**anchored to the config file's directory** here, before serialization —
so the workflow's worker process sees absolute paths regardless of which
directory it was launched from.

Without this anchoring, the worker's CWD silently picks the artifact root,
which depends on how the user invoked `python -m prod.live.worker` and is a
recipe for KB output landing in surprising places.

vLLM URL resolution does NOT happen here — `vllm-instance://` URLs propagate
through workflow input so the worker resolves at activity boundary using its
own `OCR_VLLM_PREFER_PRIVATE_IP` setting. In-process callers (`pipeline/cli.py`)
explicitly invoke `resolve_config_urls` before use.
"""

from pathlib import Path

import yaml


_RELATIVE_PATH_FIELDS: tuple[tuple[str, ...], ...] = (
    ("output", "kb_root"),
    ("output", "assets_uri_prefix"),  # only anchored when local; s3:// values pass through
)


def _anchor_relative_paths(cfg: dict, config_dir: Path) -> None:
    """Resolve relative path fields in `cfg` against `config_dir`. In-place.

    Skips s3:// (and other URL-scheme) values — they're already absolute references.
    """
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
        if "://" in raw:
            continue
        p = Path(raw)
        if not p.is_absolute():
            node[leaf] = str((config_dir / p).resolve())


def load_pipeline_config(config_path: str | Path) -> dict:
    """Load a pipeline YAML and anchor relative paths. URLs stay unresolved."""
    config_path = Path(config_path).resolve()
    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}
    config_dir = config_path.parent
    cfg["_config_dir"] = str(config_dir)
    _anchor_relative_paths(cfg, config_dir)
    return cfg


def apply_prod_overlay(config: dict, overlay_path: str | Path) -> dict:
    """Layer a prod overlay onto a base pipeline config (in-place + return).

    The overlay YAML must have the shape produced by `prod/live/config/prod_config.yaml`:
      pipeline_overrides:
        <section>:
          <key>: <value>      # None entries are ignored (lets overlays "clear" defaults)

    pipeline_overrides is merged one level deep (section dicts get `.update(...)`d).

    Single source of truth for the overlay merge: live's SQS consumer and
    batch's `cli submit` both go through here. Missing overlay file raises
    FileNotFoundError so misconfiguration is loud, not silent.
    """
    overlay_path = Path(overlay_path).resolve()
    with open(overlay_path) as f:
        overlay = yaml.safe_load(f) or {}

    overrides = overlay.get("pipeline_overrides", {})
    for section, values in overrides.items():
        if isinstance(values, dict):
            config.setdefault(section, {}).update(
                {k: v for k, v in values.items() if v is not None}
            )
        else:
            config[section] = values

    return config
