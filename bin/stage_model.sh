#!/usr/bin/env bash
# hf download -> verify no .incomplete -> aws s3 sync -> write .done sentinel.
# Idempotent: skips tuples with .done unless --force. vLLM boxes refuse to
# boot without .done, so a half-upload never serves.
#
#   bin/stage_model.sh <hf_id> [<revision>]      # revision defaults to "main"
#   bin/stage_model.sh --all [--force]           # stage every tuple in shared/vllm var.models
#
# Requires: hf CLI (pip install huggingface_hub), aws, jq, terraform.
# HF token: $HF_TOKEN wins, else SSM /ocr-bench/hf_token.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF="$REPO_ROOT/bin/tf.sh"
HF_TOKEN_SSM_PARAM="/ocr-bench/hf_token"
AWS_REGION="${AWS_REGION:-us-west-2}"

need() { command -v "$1" >/dev/null || { echo "error: $1 not on PATH" >&2; exit 1; }; }
need aws; need jq; need terraform

usage() {
  cat <<EOF
usage: bin/stage_model.sh <hf_id> [<revision>] [--force]
       bin/stage_model.sh --all [--force]

  <hf_id>     HF repo id, e.g. google/gemma-4-12b-it
  <revision>  S3 subdir (default "main"). Branch, tag, or pinned SHA.
              Must match hf_revision in shared/vllm/terraform/variables.tf.
  --all       Every (hf_id, hf_revision) tuple in var.models. Skips .done.
  --force     Re-stage even if .done exists.
EOF
  exit 1
}

force=0
mode="single"
targets=()
if [[ $# -eq 0 ]]; then usage; fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)   mode="all"; shift ;;
    --force) force=1; shift ;;
    -h|--help) usage ;;
    -*)      echo "error: unknown flag '$1'" >&2; usage ;;
    *)
      [[ $mode == "all" ]] && { echo "error: no positional args with --all" >&2; usage; }
      if [[ ${#targets[@]} -eq 0 ]]; then targets=("$1" "main")
      elif [[ ${#targets[@]} -eq 2 ]]; then targets[1]="$1"
      else echo "error: too many positional args" >&2; usage
      fi
      shift ;;
  esac
done

echo "==> resolving weights bucket via shared/platform tfstate"
BUCKET=$("$TF" shared/platform output -raw vllm_weights_bucket 2>/dev/null || true)
[[ -n "$BUCKET" ]] || {
  echo "error: could not read 'vllm_weights_bucket' output from shared/platform." >&2
  echo "       run 'bin/tf.sh shared/platform init && bin/tf.sh shared/platform apply' first." >&2
  exit 1
}
echo "    bucket: $BUCKET"

resolve_hf_token() {
  if [[ -n "${HF_TOKEN:-}" ]]; then echo "$HF_TOKEN"; return; fi
  local tok
  tok=$(aws ssm get-parameter --region "$AWS_REGION" \
        --name "$HF_TOKEN_SSM_PARAM" --with-decryption \
        --query Parameter.Value --output text 2>/dev/null || true)
  if [[ -z "$tok" || "$tok" == "None" ]]; then
    echo "error: no HF token. set \$HF_TOKEN or populate SSM param $HF_TOKEN_SSM_PARAM." >&2
    echo "  aws ssm put-parameter --region $AWS_REGION --overwrite --type SecureString \\" >&2
    echo "      --name $HF_TOKEN_SSM_PARAM --value \"\$YOUR_HF_READ_TOKEN\"" >&2
    exit 1
  fi
  echo "$tok"
}

stage_one() {
  local hf_id="$1" revision="$2"
  local s3_uri="s3://$BUCKET/models/$hf_id/$revision"

  echo
  echo "==> $hf_id @ $revision"
  if [[ $force -eq 0 ]] && aws s3 ls "$s3_uri/.done" >/dev/null 2>&1; then
    echo "    .done present — skipping (use --force to re-stage)"
    return 0
  fi

  need hf

  local staging
  staging=$(mktemp -d -t "vllm-stage-XXXXXX")
  trap "rm -rf '$staging'" RETURN

  echo "    hf download -> $staging"
  local token; token=$(resolve_hf_token)
  # --local-dir => real files (not symlinks) for aws s3 sync.
  HF_TOKEN="$token" HF_HUB_DOWNLOAD_TIMEOUT="${HF_HUB_DOWNLOAD_TIMEOUT:-300}" \
    hf download "$hf_id" --revision "$revision" --local-dir "$staging"

  # Any .incomplete => stalled download; don't publish.
  if find "$staging" -type f -name '*.incomplete' -print -quit | grep -q .; then
    echo "error: .incomplete files remain in $staging." >&2
    return 1
  fi
  [[ -s "$staging/config.json" ]] || {
    echo "error: $staging/config.json missing — not a valid HF repo." >&2
    return 1
  }

  echo "    aws s3 sync -> $s3_uri/"
  # --delete drops stale files if operator re-stages a smaller revision here.
  aws s3 sync "$staging/" "$s3_uri/" --delete --only-show-errors

  # .done LAST — user_data checks it before sync, so an in-flight stage never boots.
  aws s3 cp - "$s3_uri/.done" --only-show-errors <<<"$(printf 'staged_at=%s\nhf_id=%s\nrevision=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$hf_id" "$revision")"

  echo "    done: $s3_uri/"
}

# --all: iterate flattened primary+secondary tuples from var.models.
stage_all() {
  echo "==> reading models config from shared/vllm/terraform"
  local tuples
  tuples=$(cd "$REPO_ROOT/shared/vllm/terraform" && \
    terraform console <<< 'jsonencode(flatten([for k, m in var.models : concat([{hf_id = m.hf_model_id, revision = m.hf_revision}], [for s in m.secondary_services : {hf_id = s.hf_model_id, revision = s.hf_revision}])]))' \
    2>/dev/null | tail -1 | sed -e 's/^"//' -e 's/"$//' -e 's/\\"/"/g')

  [[ -n "$tuples" && "$tuples" != "[]" ]] || {
    echo "error: no models found in shared/vllm/terraform var.models" >&2
    exit 1
  }

  local n; n=$(echo "$tuples" | jq 'length')
  echo "    found $n tuples"

  local i
  for (( i=0; i<n; i++ )); do
    stage_one "$(echo "$tuples" | jq -r ".[$i].hf_id")" "$(echo "$tuples" | jq -r ".[$i].revision")"
  done
}

if [[ $mode == "all" ]]; then
  stage_all
else
  [[ ${#targets[@]} -eq 2 ]] || usage
  stage_one "${targets[0]}" "${targets[1]}"
fi

echo
echo "stage_model.sh: complete."
