#!/usr/bin/env bash
# Tear the batch motif down.
#
#   batch           destroy   →  Lambda + S3 notification + ASGs + IAM + DLQ
#   common/vllm     destroy   →  vLLM box
#   common/temporal destroy   →  cpu-pipeline-01 (SSM parameters survive — they're
#                                created here too but holding tree_llm secrets;
#                                see bin/README.md if a full account wipe is wanted)
#
# Safe to re-run. Each destroy is a no-op if the resource doesn't exist.

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
"$TF" batch destroy -auto-approve -input=false "${extra_args[@]}"

step "common/vllm destroy"
"$TF" common/vllm destroy -auto-approve -input=false -var "env_tag=prod" "${extra_args[@]}"

step "common/temporal destroy"
"$TF" common/temporal destroy -auto-approve -input=false "${extra_args[@]}"

echo
echo "batch motif down. SSM tree_llm parameters retained — see bin/README.md if you need to clear them."
