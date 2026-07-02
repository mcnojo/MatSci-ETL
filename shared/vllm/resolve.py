"""Resolve vllm-instance:// URLs against EC2 instance tags.

URL form:           vllm-instance://<model>:<port>/<path>
EC2 tag filter:     state=running AND role = vllm-<model>

shared/vllm/terraform provisions one instance per entry in var.models, each
tagged role=vllm-<model_key>. A caller writes
vllm-instance://chandra:8004/v1 for the OCR endpoint and
vllm-instance://gemma:8005/v1 for tree_llm; each resolves to a different IP.

Env knob:
    OCR_VLLM_PREFER_PRIVATE_IP  - 1 to return private IP (default: 0 — public)

Mac-side callers (operator's laptop) leave it unset -> resolves to the public
IP. Worker user-data sets PREFER_PRIVATE_IP=1 so in-VPC workers route over
the private network.
"""

import os
import time
from threading import Lock
from urllib.parse import urlparse

import boto3


_CACHE_TTL_S = 300

_cache: dict[str, tuple[dict, float]] = {}
_cache_lock = Lock()


def _truthy(s: str | None) -> bool:
    return (s or "").strip().lower() in ("1", "true", "yes", "on")


def _describe_vllm_instance(model: str) -> dict:
    """Return the running EC2 instance tagged role=vllm-<model>.

    Result is cached for `_CACHE_TTL_S` so a long-running worker doesn't
    hit the EC2 API on every call. Raises if zero or multiple matches.
    """
    now = time.time()
    with _cache_lock:
        entry = _cache.get(model)
        if entry and (now - entry[1]) < _CACHE_TTL_S:
            return entry[0]

    role_tag = f"vllm-{model}"
    ec2 = boto3.client("ec2")
    resp = ec2.describe_instances(Filters=[
        {"Name": "tag:role", "Values": [role_tag]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ])
    instances = [
        i for r in resp.get("Reservations", []) for i in r.get("Instances", [])
    ]

    if not instances:
        raise LookupError(
            f"no running EC2 instance tagged role={role_tag!r}. "
            f"apply shared/vllm first."
        )
    if len(instances) > 1:
        ids = [i["InstanceId"] for i in instances]
        raise LookupError(
            f"multiple running instances tagged role={role_tag!r}: {ids}. "
            f"terminate the extras."
        )
    inst = instances[0]
    with _cache_lock:
        _cache[model] = (inst, now)
    return inst


def resolve_vllm_url(url: str, *, prefer_private_ip: bool | None = None) -> str:
    """Resolve `vllm-instance://<model>:<port>/<path>` to `http://<ip>:<port>/<path>`.

    Non-`vllm-instance://` URLs pass through unchanged — safe to call on already-
    resolved URLs (idempotent at activity boundaries).
    """
    if not url.startswith("vllm-instance://"):
        return url

    if prefer_private_ip is None:
        prefer_private_ip = _truthy(os.environ.get("OCR_VLLM_PREFER_PRIVATE_IP"))

    parsed = urlparse(url)
    model = parsed.hostname
    if not model:
        raise ValueError(f"bad vllm-instance URL (no host): {url!r}")

    inst = _describe_vllm_instance(model)
    ip = inst.get("PrivateIpAddress") if prefer_private_ip else inst.get("PublicIpAddress")
    if not ip:
        which = "private" if prefer_private_ip else "public"
        raise LookupError(
            f"instance {inst['InstanceId']} has no {which} IP. "
            f"check the SG/VPC config in shared/vllm."
        )

    port = f":{parsed.port}" if parsed.port else ""
    return f"http://{ip}{port}{parsed.path or ''}"


def resolve_config_urls(config: dict) -> dict:
    """Resolve any vllm-instance:// URLs in a pipeline config dict (in-place)."""
    for section in ("vision_server", "tree_llm"):
        cfg = config.get(section, {})
        if isinstance(cfg.get("base_url"), str):
            cfg["base_url"] = resolve_vllm_url(cfg["base_url"])
    return config


def clear_cache() -> None:
    """Test hook — drop the EC2 lookup cache."""
    with _cache_lock:
        _cache.clear()
