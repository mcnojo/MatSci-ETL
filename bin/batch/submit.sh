#!/usr/bin/env bash
# Upload a folder of PDFs as a batch (does NOT start the workflow — that's
# `python -m prod.batch.cli submit <batch_id>` printed at the end).
#
#   bin/batch/submit.sh <folder> [--batch-id ID] [--config-overrides path.json]
#
# Flow:
#   1. Enumerate *.pdf in <folder>
#   2. Build manifest in memory; validate via BatchManifest.model_validate
#   3. Upload PDFs to s3://<bucket>/batches/incoming/<batch_id>/pdfs/
#   4. Upload manifest.json to s3://<bucket>/batches/incoming/<batch_id>/manifest.json
#   5. Print the batch_id and the exact command to start the workflow.
#
# Batch ID defaults to <folder-basename>-<utc-yyyymmdd-hhmm> so accidental
# re-runs don't collide with the previous workflow (which would be rejected
# by REJECT_DUPLICATE).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# --- args --------------------------------------------------------------------
folder=""
batch_id=""
config_overrides=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --batch-id)         batch_id="$2"; shift 2 ;;
    --config-overrides) config_overrides="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,16p' "$0"; exit 0 ;;
    -*)
      echo "unknown flag: $1" >&2; exit 1 ;;
    *)
      if [[ -n "$folder" ]]; then echo "extra positional: $1" >&2; exit 1; fi
      folder="$1"; shift ;;
  esac
done
[[ -n "$folder" && -d "$folder" ]] || { echo "usage: $0 <folder> [--batch-id ID] [--config-overrides path]" >&2; exit 1; }

# --- discover ----------------------------------------------------------------
need() { command -v "$1" >/dev/null || { echo "error: $1 not on PATH" >&2; exit 1; }; }
need aws
need terraform

PYTHON="${REPO_ROOT}/env/bin/python"
[[ -x "$PYTHON" ]] || { echo "error: venv not found at $PYTHON — run: python3 -m venv env && env/bin/pip install -e ." >&2; exit 1; }

# Guard: batch motif must be up. ASG existence is the canonical indicator —
# if `terraform output` returns nothing, bin/batch/up.sh hasn't been run.
_asg_name=$(terraform -chdir="$REPO_ROOT/prod/batch/terraform" output -raw cpu_queue_asg_name 2>/dev/null || true)
if [[ -z "$_asg_name" ]]; then
  echo "error: batch motif is not up — run bin/batch/up.sh first" >&2
  exit 1
fi

pdfs=()
while IFS= read -r -d '' f; do pdfs+=("$f"); done < <(find "$folder" -maxdepth 1 -type f -name "*.pdf" -print0 | sort -z)
[[ ${#pdfs[@]} -gt 0 ]] || { echo "no PDFs found in $folder" >&2; exit 1; }
echo "discovered ${#pdfs[@]} PDFs in $folder"

artifact_bucket=$(terraform -chdir="$REPO_ROOT/prod/batch/terraform" output -raw artifact_bucket)
# INCOMING_PREFIX is the manifest-layout convention shared with the CLI submit
# step; sourced from the Python constant so there's exactly one source of truth.
incoming_prefix=$(PYTHONPATH="$REPO_ROOT" "$PYTHON" -c 'from prod.batch.planner import INCOMING_PREFIX; print(INCOMING_PREFIX)')
report_root=$(terraform -chdir="$REPO_ROOT/prod/batch/terraform" output -raw batch_report_root 2>/dev/null || true)
temporal_ui_host=$(terraform -chdir="$REPO_ROOT/shared/temporal/terraform" output -raw cpu_pipeline_public_ip)

if [[ -z "$batch_id" ]]; then
  base=$(basename "$folder" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-_' '-' | sed 's/-\+/-/g; s/^-//; s/-$//')
  batch_id="${base}-$(date -u +%Y%m%d-%H%M)"
fi
echo "batch_id: $batch_id"

# --- manifest ----------------------------------------------------------------
# Build + validate the manifest in Python so document_id sanitization +
# BatchManifest validation share code with the workflow. Catches duplicate
# IDs, bad characters, and missing config_overrides JSON up-front.
manifest_path=$(mktemp -t "${batch_id}.manifest.XXXXXX.json")
trap 'rm -f "$manifest_path"' EXIT

PYTHONPATH="$REPO_ROOT" "$PYTHON" - \
  "$artifact_bucket" "$incoming_prefix" "$batch_id" "$manifest_path" "$config_overrides" "${pdfs[@]}" <<'PY'
import json, re, sys
from pathlib import Path
from prod.batch.models import BatchManifest, BatchItem

bucket, prefix, batch_id, manifest_out, overrides_path, *paths = sys.argv[1:]

def sanitize(stem: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return s or "unknown"

items = []
seen = {}
for p in paths:
    stem = Path(p).stem
    doc_id = sanitize(stem)
    if doc_id in seen:
        raise SystemExit(
            f"document_id collision: '{Path(p).name}' and '{Path(seen[doc_id]).name}' "
            f"both sanitize to '{doc_id}'"
        )
    seen[doc_id] = p
    items.append(BatchItem(
        document_id=doc_id,
        pdf_uri=f"s3://{bucket}/{prefix}{batch_id}/pdfs/{doc_id}.pdf",
    ))

overrides = json.loads(Path(overrides_path).read_text()) if overrides_path else None
manifest = BatchManifest(batch_id=batch_id, items=items, config_overrides=overrides)
Path(manifest_out).write_text(manifest.model_dump_json(indent=2))
print(f"manifest validated: {len(items)} items")
PY

# --- upload ------------------------------------------------------------------
sanitize_id() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed 's/-\+/-/g; s/^-//; s/-$//'
}

echo
echo "uploading ${#pdfs[@]} PDFs to s3://$artifact_bucket/$incoming_prefix$batch_id/pdfs/"
for p in "${pdfs[@]}"; do
  doc_id=$(sanitize_id "$(basename "$p" .pdf)")
  aws s3 cp --only-show-errors "$p" "s3://$artifact_bucket/$incoming_prefix$batch_id/pdfs/$doc_id.pdf"
done

echo
echo "uploading manifest -> s3://$artifact_bucket/$incoming_prefix$batch_id/manifest.json"
aws s3 cp --only-show-errors "$manifest_path" "s3://$artifact_bucket/$incoming_prefix$batch_id/manifest.json"

# --- report ------------------------------------------------------------------
echo
echo "uploaded. start the workflow with:"
echo
echo "  python -m prod.batch.cli submit $batch_id"
echo
echo "  workflow id  : batch-$batch_id"
echo "  temporal ui  : http://$temporal_ui_host:8233/namespaces/default/workflows/batch-$batch_id"
if [[ -n "$report_root" ]]; then
  echo "  report uri   : $report_root/batches/$batch_id/report/report.{json,md}"
fi
