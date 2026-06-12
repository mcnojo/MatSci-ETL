#!/usr/bin/env bash
# Thin wrapper so operators don't `cd` between terraform modules.
#
#   bin/tf.sh <module> <action> [args...]
#
# Modules: batch | live | shared/temporal | shared/vllm
# Action passes straight through to terraform. On `init`, the shared backend
# config (shared/terraform/_backend.hcl) is auto-supplied so every module
# stores state in the same bucket under its own key.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_CONFIG="$REPO_ROOT/shared/terraform/_backend.hcl"

usage() {
  cat <<EOF
usage: bin/tf.sh <module> <action> [args...]

  <module>   one of: batch, live, shared/platform, shared/temporal, shared/vllm
  <action>   any terraform subcommand (init, plan, apply, destroy, output, ...)

  bin/tf.sh batch init
  bin/tf.sh batch plan
  bin/tf.sh shared/vllm apply -var env_tag=dev
EOF
  exit 1
}

[[ $# -lt 2 ]] && usage

MODULE="$1"
ACTION="$2"
shift 2

case "$MODULE" in
  batch)            MODULE_DIR="$REPO_ROOT/prod/batch/terraform" ;;
  live)             MODULE_DIR="$REPO_ROOT/prod/live/terraform" ;;
  shared/platform)  MODULE_DIR="$REPO_ROOT/shared/platform/terraform" ;;
  shared/temporal)  MODULE_DIR="$REPO_ROOT/shared/temporal/terraform" ;;
  shared/vllm)      MODULE_DIR="$REPO_ROOT/shared/vllm/terraform" ;;
  *) echo "error: unknown module '$MODULE' (expected: batch, live, shared/platform, shared/temporal, shared/vllm)" >&2; exit 1 ;;
esac

[[ -d "$MODULE_DIR" ]] || { echo "error: no terraform module at $MODULE_DIR" >&2; exit 1; }
[[ -f "$BACKEND_CONFIG" ]] || { echo "error: missing $BACKEND_CONFIG" >&2; exit 1; }

if ! command -v terraform >/dev/null; then
  echo "error: terraform CLI not on PATH" >&2; exit 1
fi

if [[ "$ACTION" == "init" ]]; then
  exec terraform -chdir="$MODULE_DIR" init -backend-config="$BACKEND_CONFIG" "$@"
fi

# Pass through. Terraform itself reports if the module needs init.
exec terraform -chdir="$MODULE_DIR" "$ACTION" "$@"
