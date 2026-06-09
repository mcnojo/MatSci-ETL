#!/usr/bin/env bash
# Tear down the env_tag=dev vLLM box. No-op if not applied.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF="$REPO_ROOT/bin/tf.sh"

"$TF" common/vllm destroy -auto-approve -input=false -var "env_tag=dev" "$@"
echo "dev vllm box down."
