#!/usr/bin/env bash
# Tear the batch motif down.
#
# Order (and why):
#   batch           destroy   ->  ASGs + worker SG + IAM + lifecycle SQS + the
#                                cross-module SG ingress rules attached to
#                                cpu_pipeline (Temporal :7233) and the vLLM SG.
#                                MUST precede shared/vllm and shared/temporal
#                                because both are referenced via remote_state
#                                outputs — destroying them first leaves batch
#                                unable to even plan.
#   shared/vllm     destroy   ->  Kills the GPU box. Could go first for the
#                                cost saving, but batch is fast to destroy
#                                (no Lambda ENIs to wait on) so the dependency-
#                                order rule wins over the few cents of g6e
#                                time.
#   shared/temporal destroy   ->  cpu-pipeline-01 + SG + EIP + log group +
#                                S3 VPC endpoint. SSM tree_llm keys are NOT
#                                here anymore — they live in shared/platform
#                                and survive nightly teardown by design.
#
# Safe to re-run. Each destroy is a no-op if the resource doesn't exist.
#
# shared/platform is intentionally NOT touched — operator-populated API key
# slots must survive teardown. Use bin/tf.sh shared/platform destroy if you
# really want to wipe them (requires removing prevent_destroy first).

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

step "batch destroy"
"$TF" batch destroy -auto-approve -input=false ${extra_args[@]+"${extra_args[@]}"}

step "shared/vllm destroy"
"$TF" shared/vllm destroy -auto-approve -input=false -var "env_tag=prod" ${extra_args[@]+"${extra_args[@]}"}

step "shared/temporal destroy"
"$TF" shared/temporal destroy -auto-approve -input=false ${extra_args[@]+"${extra_args[@]}"}

echo
echo "batch motif down. shared/platform (SSM key slots) intentionally untouched."
