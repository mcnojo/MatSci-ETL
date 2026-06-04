#!/usr/bin/env bash
# Graceful spin-down of the AWS pipeline.
#
# 1. Stop the GPU box (model server)
# 2. Stop the CPU box (Temporal + Postgres + worker)
#
# Usage:
#   ./prod/live/scripts/spin_down.sh
#
# The Elastic IP on the CPU box reattaches automatically on next start.
# GPU box IP will change — the resolver handles this.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$REPO_ROOT/vllm/aws/config.sh"

# Resolve instance IDs

GPU_ID_FILE="$INSTANCE_DIR/chandra.id"
GPU_REGION_FILE="$INSTANCE_DIR/chandra.region"

if [[ ! -f "$GPU_ID_FILE" ]]; then
    echo "No GPU instance tracked (no $GPU_ID_FILE). Skipping GPU."
    GPU_ID=""
else
    GPU_ID=$(cat "$GPU_ID_FILE")
    GPU_REGION=$(cat "$GPU_REGION_FILE" 2>/dev/null || echo "$AWS_REGION")
fi

# CPU instance — look up by tag since it's not tracked in vllm/aws/instances
CPU_ID=$(aws ec2 describe-instances \
    --region "$AWS_REGION" \
    --filters "Name=tag:Name,Values=ocr-bench-cpu-pipeline-01" \
              "Name=instance-state-name,Values=running,stopped" \
    --query "Reservations[0].Instances[0].InstanceId" \
    --output text 2>/dev/null || true)

echo "GPU instance: ${GPU_ID:-none}"
echo "CPU instance: ${CPU_ID:-none}"
echo ""

# 1. Stop GPU box

if [[ -n "$GPU_ID" && "$GPU_ID" != "None" ]]; then
    echo "Stopping GPU instance $GPU_ID..."
    aws ec2 stop-instances --region "${GPU_REGION:-$AWS_REGION}" --instance-ids "$GPU_ID" >/dev/null
    echo "GPU stop initiated"
else
    echo "No GPU instance to stop"
fi

# 2. Stop CPU box

if [[ -n "$CPU_ID" && "$CPU_ID" != "None" ]]; then
    echo "Stopping CPU instance $CPU_ID..."
    aws ec2 stop-instances --region "$AWS_REGION" --instance-ids "$CPU_ID" >/dev/null
    echo "CPU stop initiated"
else
    echo "No CPU instance to stop"
fi

echo ""
echo "Spin-down complete. Instances are stopping (not terminated)."
echo "EBS volumes are preserved. Resume with: ./prod/live/scripts/spin_up.sh"
