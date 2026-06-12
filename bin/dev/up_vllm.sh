#!/usr/bin/env bash
# Hybrid local-dev escape hatch: bring up ONLY the vLLM box, tagged env=dev.
# Operator's Mac continues to drive etl/cli.py with Ollama running locally;
# the vision_server resolves to this box via EC2 tag lookup (role=vllm-<model>-dev).
#
# No Temporal, no SSM secrets, no batch fleet — those are shared/temporal +
# shared/vllm(env_tag=prod) + live/batch territory.
#
# Flags:
#   --zone <az>           AZ shortcut (e.g. us-west-2a) to dodge capacity stalls.
#   --operator-cidr <c>   CIDR allowed inbound on vLLM ports. Repeatable.
#                         If omitted, auto-detected via checkip.amazonaws.com.
#   -- <args...>          Raw terraform passthrough.

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

# Default the operator CIDR to this host's public IP if not given. Hard-fail
# if detection doesn't respond — never apply with an empty list, which would
# (correctly, post-fix) leave the box unreachable from the Mac.
if [[ ${#operator_cidrs[@]} -eq 0 ]]; then
  detected=$(curl -fsS --max-time 5 https://checkip.amazonaws.com | tr -d '[:space:]') || detected=""
  if [[ -z "$detected" ]]; then
    echo "error: --operator-cidr not given and checkip.amazonaws.com did not respond." >&2
    echo "       pass --operator-cidr <your_cidr>/32 explicitly and re-run." >&2
    exit 1
  fi
  operator_cidrs=("$detected/32")
  echo "auto-detected operator cidr: ${operator_cidrs[0]} (override with --operator-cidr)"
fi

vllm_args=()
quoted=$(printf '"%s",' "${operator_cidrs[@]}"); quoted="[${quoted%,}]"
vllm_args+=("-var" "operator_cidrs=$quoted")
if [[ -n "$zone" ]]; then
  vllm_args+=("-var" "availability_zone=$zone")
fi

step() { printf "\n=== %s ===\n" "$*"; }

step "shared/vllm init + apply (env_tag=dev)"
"$TF" shared/vllm init -input=false -upgrade
"$TF" shared/vllm apply -auto-approve -input=false -var "env_tag=dev" \
    ${vllm_args[@]+"${vllm_args[@]}"} \
    ${extra_args[@]+"${extra_args[@]}"}

step "wait_health (vllm only)"
"$REPO_ROOT/bin/wait_health.sh" vllm

echo
public_ip=$(terraform -chdir="$REPO_ROOT/shared/vllm/terraform" output -raw public_ip)
port=$(terraform -chdir="$REPO_ROOT/shared/vllm/terraform" output -raw vllm_port)
echo "dev vllm box up at http://$public_ip:$port"
echo "verify:  curl -s http://$public_ip:$port/health"
echo "tear down: bin/dev/down_vllm.sh"
