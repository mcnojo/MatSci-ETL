"""Resolve vllm-instance:// URLs to http:// using tracked instance IPs."""

from pathlib import Path
from urllib.parse import urlparse

_REPO_ROOT = Path(__file__).resolve().parents[1]
_INSTANCE_DIR = _REPO_ROOT / "vllm" / "aws" / "instances"


def resolve_vllm_url(url: str) -> str:
    """Resolve ``vllm-instance://<name>:<port>/<path>`` to ``http://<ip>:<port>/<path>``.

    Reads the IP from ``vllm/aws/instances/<name>.ip`` (written by launch.sh).
    Non-vllm-instance URLs pass through unchanged.
    """
    if not url.startswith("vllm-instance://"):
        return url
    parsed = urlparse(url)
    name = parsed.hostname
    if not name:
        raise ValueError(f"Bad vllm-instance URL (no host): {url!r}")
    ip_file = _INSTANCE_DIR / f"{name}.ip"
    if not ip_file.exists():
        raise FileNotFoundError(
            f"No IP file at {ip_file}. Run `vllm/aws/launch.sh {name}` first, "
            f"or set vision_server.base_url to a literal http://... URL."
        )
    ip = ip_file.read_text().strip()
    if not ip:
        raise ValueError(f"{ip_file} is empty")
    port = f":{parsed.port}" if parsed.port else ""
    return f"http://{ip}{port}{parsed.path or ''}"


def resolve_config_urls(config: dict) -> dict:
    """Resolve any vllm-instance:// URLs in a pipeline config dict (in-place)."""
    vs = config.get("vision_server", {})
    if isinstance(vs.get("base_url"), str):
        vs["base_url"] = resolve_vllm_url(vs["base_url"])
    return config
