#!/usr/bin/env bash
# Delete every log stream in the given CloudWatch log group(s). Groups + IAM
# grants + retention are left intact, so future logs flow normally if/when the
# CWAgent comes back up.
#
#   bin/cw-logs-clear.sh                     # both module groups (TF outputs)
#   bin/cw-logs-clear.sh shared/temporal     # one module
#   bin/cw-logs-clear.sh batch
#   bin/cw-logs-clear.sh --group /custom/lg  # explicit group, repeatable
#   bin/cw-logs-clear.sh -y ...              # skip confirmations
#
# Tunables (env): AWS_REGION (default us-west-2).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF="$REPO_ROOT/bin/tf.sh"
REGION="${AWS_REGION:-us-west-2}"

need() { command -v "$1" >/dev/null || { echo "error: $1 not on PATH" >&2; exit 1; }; }
need aws
need terraform

assume_yes=false
explicit_groups=()
modules=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes)   assume_yes=true; shift ;;
    --group)    explicit_groups+=("$2"); shift 2 ;;
    -h|--help)  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *)          modules+=("$1"); shift ;;
  esac
done

# Default: both module groups if no module/group filter was given.
if [[ ${#modules[@]} -eq 0 && ${#explicit_groups[@]} -eq 0 ]]; then
  modules=("shared/temporal" "batch")
fi

# Resolve TF-output groups for any requested modules. ${arr[@]+...} guards
# against macOS bash 3.2 tripping `set -u` on empty-array expansion.
groups=()
[[ ${#explicit_groups[@]} -gt 0 ]] && groups=("${explicit_groups[@]}")
for m in "${modules[@]+"${modules[@]}"}"; do
  case "$m" in
    shared/temporal|batch) ;;
    *) echo "error: unknown module '$m' (expected: shared/temporal, batch)" >&2; exit 1 ;;
  esac
  g=$("$TF" "$m" output -raw log_group_name 2>/dev/null) || {
    echo "error: couldn't read log_group_name from module $m -- has it been applied?" >&2
    exit 1
  }
  [[ -n "$g" ]] || { echo "error: module $m has empty log_group_name output" >&2; exit 1; }
  groups+=("$g")
done

# Confirm, then nuke streams one group at a time.
echo "About to delete every log stream in:"
for g in "${groups[@]}"; do echo "  - $g (region $REGION)"; done
if ! $assume_yes; then
  read -r -p "Proceed? [y/N] " ans
  [[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "aborted"; exit 1; }
fi

for g in "${groups[@]}"; do
  echo "[$g] enumerating streams..."
  # AWS CLI v2 auto-paginates describe-log-streams; one --output text call
  # returns every stream name across pages.
  streams=$(aws logs describe-log-streams \
    --region "$REGION" \
    --log-group-name "$g" \
    --query 'logStreams[].logStreamName' \
    --output text 2>/dev/null) || {
    echo "[$g] ERROR: describe-log-streams failed (does the group exist?)" >&2
    continue
  }
  if [[ -z "$streams" ]]; then
    echo "[$g] already empty"
    continue
  fi
  count=0
  for s in $streams; do
    aws logs delete-log-stream --region "$REGION" --log-group-name "$g" --log-stream-name "$s"
    count=$((count + 1))
  done
  echo "[$g] deleted $count stream(s)"
done

echo "done"
