#!/usr/bin/env bash
# Bring the batch motif online.
#
#   shared/temporal apply  →  cpu-pipeline-01 (Temporal, worker, ingestion unit)
#   shared/vllm     apply  →  vLLM box (env_tag=prod)
#   batch           apply  →  ASGs (paused at 0) + Lambda trigger + S3 notification
#   wait_health            →  poll Temporal :7233 + vLLM /health
#
# Idempotent: re-runs are no-ops if nothing changed.
#
# Flags:
#   --zone <az>           AZ shortcut for the vLLM box (e.g. us-west-2a),
#                         used to dodge g6e capacity stalls.
#   --operator-cidr <c>   CIDR allowed inbound on Temporal UI/gRPC + vLLM
#                         ports (e.g. 24.19.235.189/32). Repeatable.
#   -- <args...>          Raw terraform passthrough applied to every apply step.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF="$REPO_ROOT/bin/tf.sh"

zone=""
operator_cidrs=()
extra_args=()

usage() {
  echo "usage: $0 [--zone <az>] [--operator-cidr <cidr>]... [-- <terraform args>]" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --zone)
      [[ $# -ge 2 ]] || usage
      zone="$2"; shift 2 ;;
    --operator-cidr)
      [[ $# -ge 2 ]] || usage
      operator_cidrs+=("$2"); shift 2 ;;
    --)
      shift; extra_args=("$@"); break ;;
    *)
      usage ;;
  esac
done

# Per-module -var argv. operator_cidrs goes to both temporal and vllm; zone
# is vllm-only. List vars become a single token: operator_cidrs=["a","b"].
temporal_args=()
vllm_args=()
if [[ ${#operator_cidrs[@]} -gt 0 ]]; then
  quoted=$(printf '"%s",' "${operator_cidrs[@]}"); quoted="[${quoted%,}]"
  temporal_args+=("-var" "operator_cidrs=$quoted")
  vllm_args+=("-var" "operator_cidrs=$quoted")
fi
if [[ -n "$zone" ]]; then
  vllm_args+=("-var" "availability_zone=$zone")
fi

step() { printf "\n=== %s ===\n" "$*"; }

# shared/platform: long-lived SSM key slots. Apply is a no-op after first run
# (lifecycle.ignore_changes preserves the operator-populated value; data
# sources downstream just read by name). Cheap to run every up; fails fast
# if the operator hasn't populated the keys yet.
step "shared/platform init + apply (key slots — survives down.sh)"
"$TF" shared/platform init -input=false -upgrade
"$TF" shared/platform apply -auto-approve -input=false ${extra_args[@]+"${extra_args[@]}"}

step "shared/temporal init + apply"
"$TF" shared/temporal init -input=false -upgrade
"$TF" shared/temporal apply -auto-approve -input=false \
    ${temporal_args[@]+"${temporal_args[@]}"} \
    ${extra_args[@]+"${extra_args[@]}"}

step "shared/vllm init + apply (env_tag=prod)"
"$TF" shared/vllm init -input=false -upgrade
"$TF" shared/vllm apply -auto-approve -input=false -var "env_tag=prod" \
    ${vllm_args[@]+"${vllm_args[@]}"} \
    ${extra_args[@]+"${extra_args[@]}"}

step "build batch_trigger Lambda bundle"
"$REPO_ROOT/prod/batch/lambdas/batch_trigger/build.sh"

step "batch init + apply"
"$TF" batch init -input=false -upgrade
"$TF" batch apply -auto-approve -input=false ${extra_args[@]+"${extra_args[@]}"}

step "wait_health"
"$REPO_ROOT/bin/wait_health.sh"

echo
echo "batch motif up. submit work with: bin/batch/submit.sh <folder>"
