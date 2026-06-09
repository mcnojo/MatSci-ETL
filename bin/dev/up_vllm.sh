#!/usr/bin/env bash
# Hybrid local-dev escape hatch: bring up ONLY the vLLM box, tagged env=dev.
# Operator's Mac continues to drive etl/cli.py with Ollama running locally;
# the vision_server resolves to this box via EC2 tag lookup (role=vllm-<model>-dev).
#
# No Temporal, no SSM secrets, no batch fleet — those are common/temporal +
# common/vllm(env_tag=prod) + live/batch territory.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF="$REPO_ROOT/bin/tf.sh"

extra_args=("$@")

step() { printf "\n=== %s ===\n" "$*"; }

step "common/vllm init + apply (env_tag=dev)"
"$TF" common/vllm init -input=false -upgrade
"$TF" common/vllm apply -auto-approve -input=false -var "env_tag=dev" "${extra_args[@]}"

step "wait_health (vllm only)"
"$REPO_ROOT/bin/wait_health.sh" vllm

echo
public_ip=$(terraform -chdir="$REPO_ROOT/infra/terraform/common/vllm" output -raw public_ip)
port=$(terraform -chdir="$REPO_ROOT/infra/terraform/common/vllm" output -raw vllm_port)
echo "dev vllm box up at http://$public_ip:$port"
echo "verify:  curl -s http://$public_ip:$port/health"
echo "tear down: bin/dev/down_vllm.sh"
