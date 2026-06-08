#!/usr/bin/env bash
# Thin wrapper so operators don't `cd` between terraform modules.
#
#   bin/tf.sh <module> <action> [args...]
#
# Modules: batch | live | common/temporal | common/vllm
# Action passes straight through to terraform. On `init`, the shared backend
# config (infra/terraform/_backend.hcl) is auto-supplied so every module
# stores state in the same bucket under its own key.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$REPO_ROOT/infra/terraform"
BACKEND_CONFIG="$TF_DIR/_backend.hcl"

usage() {
  cat <<EOF
usage: bin/tf.sh <module> <action> [args...]

  <module>   one of: batch, live, common/temporal, common/vllm
  <action>   any terraform subcommand (init, plan, apply, destroy, output, ...)

  bin/tf.sh batch init
  bin/tf.sh batch plan
  bin/tf.sh common/vllm apply -var env_tag=dev
EOF
  exit 1
}

[[ $# -lt 2 ]] && usage

MODULE="$1"
ACTION="$2"
shift 2

MODULE_DIR="$TF_DIR/$MODULE"
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
