#!/usr/bin/env bash
# Bring the live motif online.
#
#   shared/temporal apply  ->  cpu-pipeline-01 (Temporal, worker, ocr-ingestion unit)
#   shared/vllm     apply  ->  vLLM box
#   live            apply  ->  SQS queue + S3 notification + SSM queue-URL handoff
#   wait_health            ->  poll Temporal :7233 + vLLM /health
#
# Idempotent.
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

# Default the operator CIDR to this host's public IP if not given. Without
# this the shared/temporal SG ingress rules (gated on length > 0) silently
# don't create — instance comes up, Temporal listens, but the operator's Mac
# can't reach :7233/:8088/:22. Hard-fail if detection doesn't respond in 5s
# so we never apply with an empty list by accident.
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

step "shared/vllm init + apply"
"$TF" shared/vllm init -input=false -upgrade
"$TF" shared/vllm apply -auto-approve -input=false \
    ${vllm_args[@]+"${vllm_args[@]}"} \
    ${extra_args[@]+"${extra_args[@]}"}

step "live init + apply"
"$TF" live init -input=false -upgrade
"$TF" live apply -auto-approve -input=false ${extra_args[@]+"${extra_args[@]}"}

step "log tail commands"
_region="${AWS_DEFAULT_REGION:-${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}}"
_temporal_instance=$("$TF" shared/temporal output -raw cpu_pipeline_instance_id 2>/dev/null || true)
_temporal_log_group=$("$TF" shared/temporal output -raw log_group_name 2>/dev/null || true)
_vllm_models_json=$("$TF" shared/vllm output -json models 2>/dev/null || echo "")
_region_flag=${_region:+" --region $_region"}

if [[ -n "$_temporal_instance" && -n "$_temporal_log_group" ]]; then
  echo "cpu-pipeline (CloudWatch — streams appear once CWAgent starts):"
  for stream in user-data ocr-worker ocr-ingestion; do
    echo "  aws logs tail $_temporal_log_group --log-stream-name-prefix $_temporal_instance/$stream --follow$_region_flag"
  done
fi
# One tail command per vLLM box. Each box runs a single `vllm serve` unit
# logging to /var/log/vllm.log.
if [[ -n "$_vllm_models_json" && "$_vllm_models_json" != "null" && "$_vllm_models_json" != "{}" ]]; then
  echo "vLLM (SSM — /var/log/vllm.log on each instance):"
  while IFS=$'\t' read -r model instance_id; do
    echo "  # $model"
    echo "  aws ssm start-session --target $instance_id$_region_flag --document-name AWS-StartInteractiveCommand --parameters 'command=[\"tail -f /var/log/vllm.log\"]'"
  done < <(echo "$_vllm_models_json" | jq -r 'to_entries[] | "\(.key)\t\(.value.instance_id)"')
fi
echo

step "wait_health"
"$REPO_ROOT/bin/wait_health.sh"

echo
echo "live motif up. submit work with: bin/live/submit.sh <file_or_folder>..."
