#!/usr/bin/env bash
# Full teardown: terminate both CPU and GPU instances, destroying EBS volumes.
#
# Use this when you're done with the pipeline for an extended period and don't
# want to pay ~$10/mo EBS storage. To resume later, reprovision from scratch
# with launch.sh + setup_cpu.sh.
# 
# The rough break even for the gpu / cpu instances is one session / day for a month, 
# any more and it makes better sense to use EBSw
#
# Usage:
#   ./prod/scripts/teardown.sh           # interactive confirmation
#   ./prod/scripts/teardown.sh --force   # skip confirmation

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$REPO_ROOT/vllm/aws/config.sh"

FORCE="${1:-}"

# Resolve instances

GPU_ID_FILE="$INSTANCE_DIR/chandra.id"
GPU_REGION_FILE="$INSTANCE_DIR/chandra.region"

GPU_ID=""
GPU_REGION="$AWS_REGION"
if [[ -f "$GPU_ID_FILE" ]]; then
    GPU_ID=$(cat "$GPU_ID_FILE")
    GPU_REGION=$(cat "$GPU_REGION_FILE" 2>/dev/null || echo "$AWS_REGION")
fi

CPU_ID=$(aws ec2 describe-instances \
    --region "$AWS_REGION" \
    --filters "Name=tag:Name,Values=ocr-bench-cpu-pipeline-01" \
              "Name=instance-state-name,Values=running,stopped,stopping" \
    --query "Reservations[0].Instances[0].InstanceId" \
    --output text 2>/dev/null || true)

echo "GPU instance: ${GPU_ID:-none}"
echo "CPU instance: ${CPU_ID:-none}"
echo ""

if [[ -z "$GPU_ID" || "$GPU_ID" == "None" ]] && [[ -z "$CPU_ID" || "$CPU_ID" == "None" ]]; then
    echo "No instances to tear down."
    exit 0
fi

# Confirm

if [[ "$FORCE" != "--force" ]]; then
    echo "This will TERMINATE both instances and DELETE their EBS volumes."
    echo "To resume later you'll need to reprovision from scratch."
    echo ""
    read -rp "Continue? [y/N] " confirm
    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
        echo "Aborted."
        exit 1
    fi
    echo ""
fi

# Terminate GPU

if [[ -n "$GPU_ID" && "$GPU_ID" != "None" ]]; then
    echo "Terminating GPU instance $GPU_ID..."
    aws ec2 terminate-instances \
        --region "$GPU_REGION" \
        --instance-ids "$GPU_ID" \
        --query "TerminatingInstances[0].CurrentState.Name" --output text
    rm -f "$INSTANCE_DIR/chandra.id" "$INSTANCE_DIR/chandra.ip" "$INSTANCE_DIR/chandra.region"
    echo "GPU terminated, tracking files cleaned up."
else
    echo "No GPU instance to terminate."
fi

# Terminate CPU

if [[ -n "$CPU_ID" && "$CPU_ID" != "None" ]]; then
    echo "Terminating CPU instance $CPU_ID..."
    aws ec2 terminate-instances \
        --region "$AWS_REGION" \
        --instance-ids "$CPU_ID" \
        --query "TerminatingInstances[0].CurrentState.Name" --output text
    echo "CPU terminated."
else
    echo "No CPU instance to terminate."
fi

echo ""
echo "Teardown complete. EBS volumes will be deleted with the instances."
echo "To reprovision:"
echo "  GPU: vllm/aws/launch.sh chandra"
echo "  CPU: (launch CPU instance, then ./prod/scripts/setup_cpu.sh)"
