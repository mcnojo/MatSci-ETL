#!/usr/bin/env bash
# Submit a batch run.
#
# Phase 3: thin wrapper around the CLI's submit command (dry-run).
# Phase 5: also scales up cpu+gpu batch ASGs before submitting and waits
#          for worker registration.
#
# Usage:
#   ./prod/batch/scripts/submit_batch.sh <manifest-uri>

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <manifest-uri>" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

cd "$REPO_ROOT"
"$REPO_ROOT/env/bin/python" -m prod.batch.cli submit --manifest "$1"
