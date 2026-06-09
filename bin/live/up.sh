#!/usr/bin/env bash
# Bring the live motif online.
#
#   common/temporal apply  →  cpu-pipeline-01 (Temporal, worker, ocr-ingestion unit)
#   common/vllm     apply  →  vLLM box (env_tag=prod)
#   live            apply  →  SQS queue + S3 notification + SSM queue-URL handoff
#   wait_health            →  poll Temporal :7233 + vLLM /health
#
# Idempotent. Extra terraform args after `--`.

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

step "common/temporal init + apply"
"$TF" common/temporal init -input=false -upgrade
"$TF" common/temporal apply -auto-approve -input=false "${extra_args[@]}"

step "common/vllm init + apply (env_tag=prod)"
"$TF" common/vllm init -input=false -upgrade
"$TF" common/vllm apply -auto-approve -input=false -var "env_tag=prod" "${extra_args[@]}"

step "live init + apply"
"$TF" live init -input=false -upgrade
"$TF" live apply -auto-approve -input=false "${extra_args[@]}"

step "wait_health"
"$REPO_ROOT/bin/wait_health.sh"

echo
echo "live motif up. submit work with: bin/live/submit.sh <file_or_folder>..."
