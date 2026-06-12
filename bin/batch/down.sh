#!/usr/bin/env bash
# Tear the batch motif down.
#
# Order (and why):
#   shared/vllm     destroy   →  Kills the GPU box FIRST. It's the expensive
#                                resource ($$$/hr) and has no cross-module
#                                dependents in the batch motif, so getting
#                                rid of it before the slow steps is the
#                                cheapest possible teardown.
#   batch           destroy   →  Lambda + S3 notification + ASGs + IAM + DLQs.
#                                The Lambda's VPC ENIs take 15-25 min to
#                                detach — that wait blocks the SG destroy.
#                                Run in parallel with shared/temporal where
#                                possible, except for the cross-module SG
#                                rules `temporal_from_trigger_lambda` /
#                                `temporal_from_workers` which target the
#                                cpu_pipeline SG. Sequential.
#   shared/temporal destroy   →  cpu-pipeline-01 + SG + EIP + log group +
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

step "shared/vllm destroy (kill GPU first)"
"$TF" shared/vllm destroy -auto-approve -input=false -var "env_tag=prod" ${extra_args[@]+"${extra_args[@]}"}

step "batch destroy (Lambda ENI cleanup takes 15-25 min)"
"$TF" batch destroy -auto-approve -input=false ${extra_args[@]+"${extra_args[@]}"}

step "shared/temporal destroy"
"$TF" shared/temporal destroy -auto-approve -input=false ${extra_args[@]+"${extra_args[@]}"}

echo
echo "batch motif down. shared/platform (SSM key slots) intentionally untouched."
