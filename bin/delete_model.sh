#!/usr/bin/env bash
# Delete staged weights from the S3 weights bucket.
#
#   bin/delete_model.sh <hf_id>              # all revisions of one model
#   bin/delete_model.sh <hf_id> <revision>   # one revision
#   bin/delete_model.sh --all                # everything under models/
#   bin/delete_model.sh --list               # list what's staged
#   bin/delete_model.sh ... --yes            # skip confirm
#   bin/delete_model.sh ... --hard           # also purge noncurrent versions (irreversible)
#
# Bucket versioned => plain rm leaves prior versions recoverable; --hard nukes them.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF="$REPO_ROOT/bin/tf.sh"

need() { command -v "$1" >/dev/null || { echo "error: $1 not on PATH" >&2; exit 1; }; }
need aws; need jq; need terraform

usage() {
  cat <<EOF
usage: bin/delete_model.sh [--yes] [--hard] --all
       bin/delete_model.sh [--yes] [--hard] <hf_id> [<revision>]
       bin/delete_model.sh --list

  --all    Delete every object under s3://<bucket>/models/.
  --hard   Also purge noncurrent versions (irreversible).
  --yes    Skip confirm.
  --list   Print what's currently staged.
EOF
  exit 1
}

mode=""; yes=0; hard=0
targets=()
[[ $# -eq 0 ]] && usage
while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)   mode="all"; shift ;;
    --list)  mode="list"; shift ;;
    --yes|-y) yes=1; shift ;;
    --hard)  hard=1; shift ;;
    -h|--help) usage ;;
    -*) echo "error: unknown flag '$1'" >&2; usage ;;
    *)
      if [[ -z "$mode" ]]; then mode="target"; fi
      [[ ${#targets[@]} -lt 2 ]] || { echo "error: too many positional args" >&2; usage; }
      targets+=("$1"); shift ;;
  esac
done
[[ -n "$mode" ]] || usage

BUCKET=$("$TF" shared/platform output -raw vllm_weights_bucket 2>/dev/null || true)
[[ -n "$BUCKET" ]] || {
  echo "error: could not read 'vllm_weights_bucket' output from shared/platform." >&2
  echo "       run 'bin/tf.sh shared/platform init && apply' first." >&2
  exit 1
}

confirm() {
  local prompt="$1"
  if [[ $yes -eq 1 ]]; then return 0; fi
  read -r -p "$prompt [type YES to confirm]: " ans
  [[ "$ans" == "YES" ]] || { echo "aborted."; exit 1; }
}

# Purge every version + delete-marker under a prefix (plain `s3 rm` only touches current).
hard_purge() {
  local prefix="$1"
  local objects
  objects=$(aws s3api list-object-versions --bucket "$BUCKET" --prefix "$prefix" \
    --query '{Objects: (Versions[].{Key: Key, VersionId: VersionId})[] || `[]`}' 2>/dev/null || echo '{"Objects":[]}')
  local markers
  markers=$(aws s3api list-object-versions --bucket "$BUCKET" --prefix "$prefix" \
    --query '{Objects: (DeleteMarkers[].{Key: Key, VersionId: VersionId})[] || `[]`}' 2>/dev/null || echo '{"Objects":[]}')

  local all
  all=$(jq -s '{Objects: (.[0].Objects + .[1].Objects)}' <(echo "$objects") <(echo "$markers"))
  local n; n=$(echo "$all" | jq '.Objects | length')
  if [[ "$n" -eq 0 ]]; then echo "    nothing to purge under $prefix"; return; fi

  # delete-objects caps at 1000/req.
  echo "$all" | jq -c '.Objects | _nwise(1000) | {Objects: .}' | \
    while IFS= read -r chunk; do
      aws s3api delete-objects --bucket "$BUCKET" --delete "$chunk" >/dev/null
    done
  echo "    hard-purged $n versions/markers under $prefix"
}

case "$mode" in
  list)
    echo "==> models staged in s3://$BUCKET/models/"
    # Lists every `.done` sentinel and its (<hf_id>/<revision>) prefix.
    aws s3 ls "s3://$BUCKET/models/" --recursive --summarize 2>/dev/null | grep -E '/\.done$' | \
      awk '{ sub(/^models\//,"",$4); sub(/\/\.done$/,"",$4); printf "  %s  (staged %s %s)\n", $4, $1, $2 }' \
      || echo "  (nothing staged)"
    ;;
  all)
    prefix="models/"
    echo "==> would delete: s3://$BUCKET/$prefix (EVERYTHING)"
    aws s3 ls "s3://$BUCKET/$prefix" | head -20
    confirm "wipe s3://$BUCKET/$prefix?"
    aws s3 rm "s3://$BUCKET/$prefix" --recursive --only-show-errors
    [[ $hard -eq 1 ]] && hard_purge "$prefix"
    echo "==> deleted."
    ;;
  target)
    hf_id="${targets[0]}"; revision="${targets[1]:-}"
    if [[ -n "$revision" ]]; then
      prefix="models/$hf_id/$revision/"
      echo "==> would delete: s3://$BUCKET/$prefix"
    else
      prefix="models/$hf_id/"
      echo "==> would delete: s3://$BUCKET/$prefix (ALL revisions)"
    fi
    aws s3 ls "s3://$BUCKET/$prefix" | head -20 || { echo "  (nothing there)"; exit 0; }
    confirm "delete s3://$BUCKET/$prefix?"
    aws s3 rm "s3://$BUCKET/$prefix" --recursive --only-show-errors
    [[ $hard -eq 1 ]] && hard_purge "$prefix"
    echo "==> deleted."
    ;;
esac
