#!/usr/bin/env bash
# Pull finalized per-PDF tree.json artifacts from S3 to local disk.
#
#   bin/pull-trees.sh [dest_dir] [-- aws-s3-sync-flags...]
#
# Default dest: ./trees/. Both live and batch lanes write to the same
# s3://<bucket>/trees/<document_id>/tree.json (see prod/live/config/prod_config.yaml
# kb_root). Trees sit outside the assets/ 3-day lifecycle rule, so this is safe
# to defer.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

dest="${1:-./trees}"; [[ $# -gt 0 ]] && shift || true
[[ "${1:-}" == "--" ]] && shift

need() { command -v "$1" >/dev/null || { echo "error: $1 not on PATH" >&2; exit 1; }; }
need aws
need terraform

artifact_bucket=$(terraform -chdir="$REPO_ROOT/shared/temporal/terraform" output -raw artifact_bucket)
src="s3://$artifact_bucket/trees/"

mkdir -p "$dest"
echo "syncing $src -> $dest"
aws s3 sync "$src" "$dest" --exclude "*" --include "*/tree.json" "$@"
echo "done."
