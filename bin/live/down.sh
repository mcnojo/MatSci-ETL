#!/usr/bin/env bash
# Tear the live motif down.
#
#   live            destroy   →  SQS queue, S3 notification, SSM queue-URL param,
#                                cross-module IAM attachment
#   common/vllm     destroy   →  vLLM box
#   common/temporal destroy   →  cpu-pipeline-01
#
# After `live destroy`, the ocr-ingestion systemd unit on cpu-pipeline-01 will
# fail its ExecStartPre (no SSM param) and restart-loop. Destroying common/temporal
# clears that immediately; if you want to keep cpu-pipeline-01 up but stop
# ingestion, just run `live destroy` and leave the loop running — Restart=always
# costs nothing meaningful.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF="$REPO_ROOT/bin/tf.sh"

extra_args=()
if [[ $# -gt 0 ]]; then
  if [[ "$1" == "--" ]]; then
    shift
    extra_args=("$@")
  else
    echo "usage: $0 [-- <extra terraform args>]" >&2
    exit 1
  fi
fi

step() { printf "\n=== %s ===\n" "$*"; }

step "live destroy"
"$TF" live destroy -auto-approve -input=false "${extra_args[@]}"

step "common/vllm destroy"
"$TF" common/vllm destroy -auto-approve -input=false -var "env_tag=prod" "${extra_args[@]}"

step "common/temporal destroy"
"$TF" common/temporal destroy -auto-approve -input=false "${extra_args[@]}"

echo
echo "live motif down."
