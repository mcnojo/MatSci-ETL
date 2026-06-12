#!/usr/bin/env bash
# Prepare the Lambda deployment bundle under build/bundle/.
#
# Terraform's archive_file data source zips that directory at plan time, so
# this script is only the source/deps layout step. Run before
# `bin/tf.sh batch apply` whenever the handler, leaf modules, or pinned
# dependency versions change.
#
# Env knobs:
#   LAMBDA_ARCH    arm64 (default) | x86_64. Must match
#                  var.lambda_architecture in prod/batch/terraform.
#   LAMBDA_PYTHON  python3.12 (default). Must match var.lambda_runtime.
#   PIP            override the pip binary (defaults to `pip3`).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../../.." && pwd)"
BUNDLE_DIR="$HERE/build/bundle"

LAMBDA_ARCH="${LAMBDA_ARCH:-arm64}"
LAMBDA_PYTHON="${LAMBDA_PYTHON:-python3.12}"
PIP_BIN="${PIP:-pip3}"

case "$LAMBDA_ARCH" in
    arm64)  pip_platform="manylinux2014_aarch64" ;;
    x86_64) pip_platform="manylinux2014_x86_64" ;;
    *)      echo "[build] unsupported LAMBDA_ARCH=$LAMBDA_ARCH" >&2; exit 2 ;;
esac

py_version="${LAMBDA_PYTHON#python}"

echo "[build] arch=$LAMBDA_ARCH python=$LAMBDA_PYTHON pip=$PIP_BIN"
echo "[build] cleaning $BUNDLE_DIR"
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR"

echo "[build] copying handler"
cp "$HERE/handler.py" "$BUNDLE_DIR/handler.py"

echo "[build] copying leaf source modules"
mkdir -p "$BUNDLE_DIR/prod/batch/workflows"
mkdir -p "$BUNDLE_DIR/shared/temporal"
: > "$BUNDLE_DIR/prod/__init__.py"
: > "$BUNDLE_DIR/prod/batch/__init__.py"
: > "$BUNDLE_DIR/prod/batch/workflows/__init__.py"
: > "$BUNDLE_DIR/shared/__init__.py"
: > "$BUNDLE_DIR/shared/temporal/__init__.py"

cp "$REPO_ROOT/prod/batch/models.py"             "$BUNDLE_DIR/prod/batch/models.py"
cp "$REPO_ROOT/prod/batch/planner.py"            "$BUNDLE_DIR/prod/batch/planner.py"
cp "$REPO_ROOT/prod/batch/workflows/models.py"   "$BUNDLE_DIR/prod/batch/workflows/models.py"
cp "$REPO_ROOT/shared/temporal/task_queues.py"   "$BUNDLE_DIR/shared/temporal/task_queues.py"
cp "$REPO_ROOT/shared/temporal/client.py"        "$BUNDLE_DIR/shared/temporal/client.py"

echo "[build] bundling pipeline configs"
cp "$REPO_ROOT/etl/config/pipeline_config.yaml"  "$BUNDLE_DIR/pipeline_config.yaml"
cp "$REPO_ROOT/prod/live/config/prod_config.yaml" "$BUNDLE_DIR/prod_config.yaml"

echo "[build] pip install --target=$BUNDLE_DIR"
"$PIP_BIN" install \
    --target "$BUNDLE_DIR" \
    --platform "$pip_platform" \
    --implementation cp \
    --python-version "$py_version" \
    --only-binary=:all: \
    --upgrade \
    --quiet \
    -r "$HERE/requirements.txt"

echo "[build] stripping bytecode + test dirs"
find "$BUNDLE_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$BUNDLE_DIR" -type d -name "tests" -prune -exec rm -rf {} +
find "$BUNDLE_DIR" -type f -name "*.pyc" -delete

size_bytes="$(du -sk "$BUNDLE_DIR" | awk '{print $1 * 1024}')"
size_mb="$(awk -v b="$size_bytes" 'BEGIN { printf "%.1f", b/1024/1024 }')"
echo "[build] done: $BUNDLE_DIR  (~$size_mb MiB)"

# Lambda zipped limit is 50MB direct upload / 250MB unpacked layer. terraform's
# archive_file writes to s3 directly (no direct-upload cap), so warn but don't
# fail on > 50MB.
if [ "$size_bytes" -gt $((50 * 1024 * 1024)) ]; then
    echo "[build] warn: bundle > 50MB unpacked — direct console upload won't work; terraform apply still will."
fi
