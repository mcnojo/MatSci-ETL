"""Operator-side resolution of the Temporal gRPC address.

Operators run prod/batch/cli and prod/reports from their laptop; the actual
Temporal server is on cpu-pipeline-01 (public EIP, port 7233 open to the
operator CIDR via shared/temporal SG). Rather than make every invocation
pass `--temporal-address <ip>:7233`, this resolver picks the right address
in priority order:

  1. explicit string (CLI flag) — always wins
  2. TEMPORAL_ADDRESS env var
  3. `terraform output -raw cpu_pipeline_public_ip` from
     shared/temporal/terraform (port 7233 appended)

Step 3 requires shared/temporal to be applied. Operators get an informative
`source` label so CLIs can show which one resolved.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Literal

Source = Literal["flag", "env", "terraform"]

_TF_DIR = Path(__file__).resolve().parent / "terraform"
_OUTPUT_KEY = "cpu_pipeline_public_ip"


def resolve_operator_address(explicit: str | None) -> tuple[str, Source]:
    """Return (address, source). See module docstring for resolution order.

    Raises `LookupError` if none of the sources yield an address — that means
    shared/temporal isn't applied and no explicit address was passed.
    """
    if explicit:
        return explicit, "flag"
    from_env = os.environ.get("TEMPORAL_ADDRESS")
    if from_env:
        return from_env, "env"
    from_tf = _read_terraform_output()
    if from_tf:
        return f"{from_tf}:7233", "terraform"
    raise LookupError(
        "no Temporal address available: pass --temporal-address, set "
        "TEMPORAL_ADDRESS, or apply shared/temporal so its terraform output "
        f"{_OUTPUT_KEY!r} is readable."
    )


def _read_terraform_output() -> str | None:
    # All failures collapse to "no output available". Subprocess error,
    # missing terraform, uninitialized module, key absent: same handling.
    try:
        result = subprocess.run(
            ["terraform", f"-chdir={_TF_DIR}", "output", "-json"],
            check=False, capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        outputs = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    entry = outputs.get(_OUTPUT_KEY)
    if not entry:
        return None
    value = entry.get("value")
    return value if isinstance(value, str) and value else None
