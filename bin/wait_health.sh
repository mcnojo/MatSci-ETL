#!/usr/bin/env bash
# Block until Temporal + vLLM are healthy. Pulls endpoints from the
# shared/temporal and shared/vllm terraform outputs (no flags, no editing).
#
#   bin/wait_health.sh                 # checks both
#   bin/wait_health.sh temporal        # only Temporal
#   bin/wait_health.sh vllm            # only vLLM
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

# nc is preferred over /dev/tcp because it's actually present everywhere and
# its return code is unambiguous.
need() { command -v "$1" >/dev/null || { echo "error: $1 not on PATH" >&2; exit 1; }; }
need terraform
need curl

tf_out() {
  local module="$1" key="$2"
  local module_dir
  case "$module" in
    temporal) module_dir="$REPO_ROOT/shared/temporal/terraform" ;;
    vllm)     module_dir="$REPO_ROOT/shared/vllm/terraform" ;;
    *) echo "tf_out: unknown module $module" >&2; return 1 ;;
  esac
  terraform -chdir="$module_dir" output -raw "$key" 2>/dev/null
}

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

if $want_temporal; then
  TEMPORAL_HOST=$(tf_out temporal cpu_pipeline_public_ip)
  if [[ -z "$TEMPORAL_HOST" ]]; then
    echo "error: shared/temporal has no cpu_pipeline_public_ip output — apply shared/temporal first" >&2
    exit 1
  fi
  TEMPORAL_PORT=7233
fi

if $want_vllm; then
  VLLM_HOST=$(tf_out vllm public_ip)
  if [[ -z "$VLLM_HOST" ]]; then
    echo "error: shared/vllm has no public_ip output — apply shared/vllm first" >&2
    exit 1
  fi
  VLLM_PORT=$(tf_out vllm vllm_port)
  VLLM_PORT="${VLLM_PORT:-8004}"
  TREE_LLM_PORT=$(tf_out vllm tree_llm_port)
  TREE_LLM_PORT="${TREE_LLM_PORT:-8005}"
fi

start=$(date +%s)
temporal_ok=$($want_temporal && echo false || echo true)
vision_ok=$($want_vllm && echo false || echo true)
tree_llm_ok=$($want_vllm && echo false || echo true)

while true; do
  now=$(date +%s)
  elapsed=$((now - start))
  if (( elapsed > deadline_s )); then
    echo "error: deadline exceeded ($deadline_s s) — temporal=$temporal_ok vision=$vision_ok tree_llm=$tree_llm_ok" >&2
    exit 1
  fi

  if ! $temporal_ok && check_temporal "$TEMPORAL_HOST" "$TEMPORAL_PORT"; then
    echo "[+${elapsed}s] temporal up ($TEMPORAL_HOST:$TEMPORAL_PORT)"
    temporal_ok=true
  fi
  if ! $vision_ok && check_vllm "$VLLM_HOST" "$VLLM_PORT"; then
    echo "[+${elapsed}s] vllm vision up ($VLLM_HOST:$VLLM_PORT/health)"
    vision_ok=true
  fi
  if ! $tree_llm_ok && check_vllm "$VLLM_HOST" "$TREE_LLM_PORT"; then
    echo "[+${elapsed}s] vllm tree_llm up ($VLLM_HOST:$TREE_LLM_PORT/health)"
    tree_llm_ok=true
  fi

  if $temporal_ok && $vision_ok && $tree_llm_ok; then
    echo "ready"
    exit 0
  fi

  echo "[+${elapsed}s] waiting (temporal=$temporal_ok vision=$vision_ok tree_llm=$tree_llm_ok) — next poll in ${interval_s}s"
  sleep "$interval_s"
done
