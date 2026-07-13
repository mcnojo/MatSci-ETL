#!/usr/bin/env bash
# One-shot vLLM box diagnostics. Reads `shared/vllm/terraform` outputs to find
# every provisioned instance, fans out one `ssm send-command` per box that
# gathers user-data trail + vllm.log tail + systemd unit state + listening
# ports, then prints each box's combined stdout.
#
# No writes. Requires: terraform, aws (SSM permissions), jq.
#
#   bin/vllm_diag.sh                # all boxes
#   bin/vllm_diag.sh chandra gemma  # subset by instance_key
#
# Tunables (env): AWS_REGION (default us-west-2, matching shared/vllm var default).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$REPO_ROOT/shared/vllm/terraform"
REGION="${AWS_REGION:-us-west-2}"

need() { command -v "$1" >/dev/null || { echo "error: $1 not on PATH" >&2; exit 1; }; }
need terraform; need aws; need jq

MODELS_JSON=$(terraform -chdir="$TF_DIR" output -json models 2>/dev/null || echo "")
if [[ -z "$MODELS_JSON" || "$MODELS_JSON" == "null" || "$MODELS_JSON" == "{}" ]]; then
  echo "error: shared/vllm has no 'models' output — apply it first" >&2
  exit 1
fi

# Restrict to requested keys, or all if none given.
if [[ $# -gt 0 ]]; then
  for k in "$@"; do
    echo "$MODELS_JSON" | jq -e --arg k "$k" 'has($k)' >/dev/null \
      || { echo "error: no such instance_key '$k' in models output" >&2; exit 1; }
  done
  keys_iter=$(printf '%s\n' "$@")
else
  keys_iter=$(echo "$MODELS_JSON" | jq -r 'keys[]')
fi

# Parallel arrays populated by the send loop. Plain `while read` for macOS bash 3.2.
keys=(); iids=(); cids=()

while IFS= read -r key; do
  entry=$(echo "$MODELS_JSON" | jq -c --arg k "$key" '.[$k]')
  iid=$(echo "$entry" | jq -r '.instance_id')
  units=$(echo "$entry" | jq -r '[.services[] | "ocr-vllm-\(.role_key).service"] | join(" ")')
  ports=$(echo "$entry" | jq -r '[.services[] | ":\(.port)"] | join("|")')

  # Build the diag script the box will run under AWS-RunShellScript.
  script=$(cat <<EOF
echo === identity ===
hostname; uptime
echo
echo === user-data tail ===
sudo tail -n 60 /var/log/user-data.log 2>&1 || echo "no user-data.log"
echo
echo === vllm log tail ===
sudo tail -n 200 /var/log/vllm.log 2>&1 || echo "no vllm.log"
echo
echo === systemd units: ${units} ===
systemctl --no-pager status ${units} 2>&1 | head -160
echo
echo === listening ports ===
sudo ss -ltnp 2>&1 | grep -E '${ports}' || echo "no vllm ports listening"
EOF
)

  # `AWS-RunShellScript` joins the `commands` array with newlines and runs the
  # result as one shell script — one array element carrying the whole thing is
  # the simplest encoding. `--cli-input-json` sidesteps shell-quoting of the
  # payload.
  input_json=$(jq -n --arg iid "$iid" --arg s "$script" '{
    InstanceIds: [$iid],
    DocumentName: "AWS-RunShellScript",
    Parameters: { commands: [$s] }
  }')

  cid=$(aws ssm send-command --region "$REGION" \
      --cli-input-json "$input_json" \
      --query 'Command.CommandId' --output text)

  echo "[$key] instance=$iid command-id=$cid"
  keys+=("$key"); iids+=("$iid"); cids+=("$cid")
done <<< "$keys_iter"

# Wait for each command to finish (Pending → InProgress → terminal).
echo
echo "waiting for commands to complete..."
for i in "${!keys[@]}"; do
  key="${keys[$i]}"; iid="${iids[$i]}"; cid="${cids[$i]}"
  while :; do
    status=$(aws ssm get-command-invocation --region "$REGION" \
        --command-id "$cid" --instance-id "$iid" \
        --query 'Status' --output text 2>/dev/null || echo "Pending")
    case "$status" in
      Success|Failed|Cancelled|TimedOut) break ;;
    esac
    sleep 2
  done
  echo "[$key] $status"
done

# Print each box's stdout, then stderr if non-empty.
for i in "${!keys[@]}"; do
  key="${keys[$i]}"; iid="${iids[$i]}"; cid="${cids[$i]}"
  echo
  echo "############### $key ($iid) ###############"
  aws ssm get-command-invocation --region "$REGION" \
      --command-id "$cid" --instance-id "$iid" \
      --query 'StandardOutputContent' --output text
  err=$(aws ssm get-command-invocation --region "$REGION" \
      --command-id "$cid" --instance-id "$iid" \
      --query 'StandardErrorContent' --output text)
  if [[ -n "$err" && "$err" != "None" ]]; then
    echo "--- stderr ---"
    echo "$err"
  fi
done
