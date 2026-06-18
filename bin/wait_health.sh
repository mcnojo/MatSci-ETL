#!/usr/bin/env bash
# Block until Temporal + every vLLM box is healthy. Pulls endpoints from the
# shared/temporal and shared/vllm terraform outputs (no flags, no editing).
#
#   bin/wait_health.sh                 # checks both
#   bin/wait_health.sh temporal        # only Temporal
#   bin/wait_health.sh vllm            # only vLLM (every model in the models output)
#
# Tunables (env): WAIT_DEADLINE_S (default 1800), WAIT_INTERVAL_S (default 10).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF="$REPO_ROOT/bin/tf.sh"

deadline_s="${WAIT_DEADLINE_S:-1800}"
interval_s="${WAIT_INTERVAL_S:-10}"

want_temporal=true
want_vllm=true
if [[ $# -gt 0 ]]; then
  case "$1" in
    temporal) want_vllm=false ;;
    vllm)     want_temporal=false ;;
    *)        echo "usage: $0 [temporal|vllm]" >&2; exit 1 ;;
  esac
fi

need() { command -v "$1" >/dev/null || { echo "error: $1 not on PATH" >&2; exit 1; }; }
need terraform
need curl
$want_vllm && need jq

# Temporal gRPC: TCP-port check. Anything more (grpcurl + DescribeNamespace)
# adds a dependency without buying meaningful certainty.
check_temporal() {
  local host="$1" port="$2"
  (echo > "/dev/tcp/$host/$port") >/dev/null 2>&1
}

check_vllm() {
  local host="$1" port="$2"
  curl -fsS --max-time 5 "http://$host:$port/health" >/dev/null 2>&1
}

# vLLM endpoints come from `terraform output -json models` — one row per
# entry in var.models. Empty output (no apply yet, or empty map) is treated
# as a misconfiguration the operator must resolve before retrying.
if $want_vllm; then
  vllm_models_json=$(terraform -chdir="$REPO_ROOT/shared/vllm/terraform" output -json models 2>/dev/null || echo "")
  if [[ -z "$vllm_models_json" || "$vllm_models_json" == "null" || "$vllm_models_json" == "{}" ]]; then
    echo "error: shared/vllm has no 'models' output (or it is empty) — apply shared/vllm first" >&2
    exit 1
  fi
  # Parallel arrays: vllm_keys / vllm_hosts / vllm_ports / vllm_ok. Built with
  # a plain `while read` loop so this runs under macOS's bash 3.2 (no mapfile).
  vllm_keys=(); vllm_hosts=(); vllm_ports=(); vllm_ok=()
  while IFS=$'\t' read -r _key _host _port; do
    vllm_keys+=("$_key")
    vllm_hosts+=("$_host")
    vllm_ports+=("$_port")
    vllm_ok+=(false)
  done < <(echo "$vllm_models_json" | jq -r 'to_entries[] | "\(.key)\t\(.value.public_ip)\t\(.value.port)"')
fi

if $want_temporal; then
  TEMPORAL_HOST=$(terraform -chdir="$REPO_ROOT/shared/temporal/terraform" output -raw cpu_pipeline_public_ip 2>/dev/null || echo "")
  if [[ -z "$TEMPORAL_HOST" ]]; then
    echo "error: shared/temporal has no cpu_pipeline_public_ip output — apply shared/temporal first" >&2
    exit 1
  fi
  TEMPORAL_PORT=7233
fi

# Format the vllm status block for log lines: chandra=true,gemma=false
status_str() {
  local out=""
  for i in "${!vllm_keys[@]}"; do
    out+="${vllm_keys[$i]}=${vllm_ok[$i]},"
  done
  echo "${out%,}"
}

# All-vllm-up: && across every entry.
all_vllm_ok() {
  for ok in "${vllm_ok[@]}"; do
    [[ "$ok" == "true" ]] || return 1
  done
  return 0
}

start=$(date +%s)
temporal_ok=$($want_temporal && echo false || echo true)
vllm_done=$($want_vllm && echo false || echo true)

while true; do
  now=$(date +%s)
  elapsed=$((now - start))
  if (( elapsed > deadline_s )); then
    if $want_vllm; then
      echo "error: deadline exceeded ($deadline_s s) — temporal=$temporal_ok vllm=[$(status_str)]" >&2
    else
      echo "error: deadline exceeded ($deadline_s s) — temporal=$temporal_ok" >&2
    fi
    exit 1
  fi

  if ! $temporal_ok && check_temporal "$TEMPORAL_HOST" "$TEMPORAL_PORT"; then
    echo "[+${elapsed}s] temporal up ($TEMPORAL_HOST:$TEMPORAL_PORT)"
    temporal_ok=true
  fi

  if $want_vllm && ! $vllm_done; then
    for i in "${!vllm_keys[@]}"; do
      if [[ "${vllm_ok[$i]}" == "false" ]] \
         && check_vllm "${vllm_hosts[$i]}" "${vllm_ports[$i]}"; then
        echo "[+${elapsed}s] vllm ${vllm_keys[$i]} up (${vllm_hosts[$i]}:${vllm_ports[$i]}/health)"
        vllm_ok[$i]=true
      fi
    done
    all_vllm_ok && vllm_done=true
  fi

  if $temporal_ok && $vllm_done; then
    echo "ready"
    exit 0
  fi

  if $want_vllm; then
    echo "[+${elapsed}s] waiting (temporal=$temporal_ok vllm=[$(status_str)]) — next poll in ${interval_s}s"
  else
    echo "[+${elapsed}s] waiting (temporal=$temporal_ok) — next poll in ${interval_s}s"
  fi
  sleep "$interval_s"
done
