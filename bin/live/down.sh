#!/usr/bin/env bash
# Tear the live motif down.
#
# Order (and why):
#   live            destroy   ->  SQS queue + S3 notification + SSM queue-URL
#                                param + cross-module IAM attachment + the
#                                cross-module SG ingress rules on the vLLM SG.
#                                MUST precede BOTH shared/vllm and shared/temporal
#                                because both are referenced via remote_state
#                                outputs — destroying them first leaves live
#                                unable to even plan.
#   shared/vllm     destroy   ->  Kills the GPU box. Could go first for the
#                                cost saving, but live destroy is fast so the
#                                dependency-order rule wins.
#   shared/temporal destroy   ->  cpu-pipeline-01 + SG + EIP + log group +
#                                S3 VPC endpoint. SSM tree_llm keys are NOT
#                                here anymore — they live in shared/platform
#                                and survive nightly teardown by design.
#
# After `live destroy`, the ocr-ingestion systemd unit on cpu-pipeline-01
# will fail its ExecStartPre (no SSM queue URL) and restart-loop. Destroying
# shared/temporal in the next step clears that immediately. If you want to
# keep cpu-pipeline-01 up but stop ingestion, just run `live destroy` and
# leave the loop running — Restart=always costs nothing meaningful.
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

step "live destroy"
"$TF" live destroy -auto-approve -input=false ${extra_args[@]+"${extra_args[@]}"}

step "shared/vllm destroy"
"$TF" shared/vllm destroy -auto-approve -input=false ${extra_args[@]+"${extra_args[@]}"}

step "shared/temporal destroy"
"$TF" shared/temporal destroy -auto-approve -input=false ${extra_args[@]+"${extra_args[@]}"}

echo
echo "live motif down. shared/platform (SSM key slots) intentionally untouched."
