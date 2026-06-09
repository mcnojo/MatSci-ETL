#!/usr/bin/env bash
# Bring the batch motif online.
#
#   common/temporal apply  →  cpu-pipeline-01 (Temporal, worker, ingestion unit)
#   common/vllm     apply  →  vLLM box (env_tag=prod)
#   batch           apply  →  ASGs (paused at 0) + Lambda trigger + S3 notification
#   wait_health            →  poll Temporal :7233 + vLLM /health
#
# Idempotent: re-runs are no-ops if nothing changed. Pass extra terraform args
# after `--`, e.g. `bin/batch/up.sh -- -var='cpu_queue_max_size=4'`.

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

step "build batch_trigger Lambda bundle"
"$REPO_ROOT/infra/lambdas/batch_trigger/build.sh"

step "batch init + apply"
"$TF" batch init -input=false -upgrade
"$TF" batch apply -auto-approve -input=false "${extra_args[@]}"

step "wait_health"
"$REPO_ROOT/bin/wait_health.sh"

echo
echo "batch motif up. submit work with: bin/batch/submit.sh <folder>"
