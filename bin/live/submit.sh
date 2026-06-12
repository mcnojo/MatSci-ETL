#!/usr/bin/env bash
# Upload PDFs to the live ingestion prefix. Each upload fires the S3 → SQS
# notification → ocr-ingestion → ProcessPdfWorkflow.
#
#   bin/live/submit.sh <file_or_folder>...
#
# Files upload as-is (preserving basename). Folders upload every *.pdf inside
# them, non-recursive (the consumer treats each PDF as an independent unit).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

[[ $# -gt 0 ]] || { echo "usage: $0 <file_or_folder>..." >&2; exit 1; }

need() { command -v "$1" >/dev/null || { echo "error: $1 not on PATH" >&2; exit 1; }; }
need aws
need terraform

artifact_bucket=$(terraform -chdir="$REPO_ROOT/shared/temporal/terraform" output -raw artifact_bucket)
incoming_prefix=$(terraform -chdir="$REPO_ROOT/prod/live/terraform" output -raw incoming_prefix 2>/dev/null || echo "live/incoming/")

pdfs=()
for arg in "$@"; do
  if [[ -d "$arg" ]]; then
    while IFS= read -r -d '' f; do pdfs+=("$f"); done \
      < <(find "$arg" -maxdepth 1 -type f -name "*.pdf" -print0 | sort -z)
  elif [[ -f "$arg" ]]; then
    [[ "$arg" == *.pdf ]] || { echo "skipping non-pdf: $arg" >&2; continue; }
    pdfs+=("$arg")
  else
    echo "not found: $arg" >&2; exit 1
  fi
done
[[ ${#pdfs[@]} -gt 0 ]] || { echo "no PDFs to upload" >&2; exit 1; }

echo "uploading ${#pdfs[@]} PDF(s) to s3://$artifact_bucket/$incoming_prefix"
for p in "${pdfs[@]}"; do
  name=$(basename "$p")
  aws s3 cp --only-show-errors "$p" "s3://$artifact_bucket/$incoming_prefix$name"
  echo "  $name"
done

echo
echo "submitted. consumer picks each up within a few seconds; watch progress at:"
temporal_ui_host=$(terraform -chdir="$REPO_ROOT/shared/temporal/terraform" output -raw cpu_pipeline_public_ip)
echo "  http://$temporal_ui_host:8233/namespaces/default/workflows"
