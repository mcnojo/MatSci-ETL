#!/usr/bin/env bash
# Spin up the AWS pipeline after a spin-down.
#
# 1. Start CPU box (Elastic IP reattaches automatically)
# 2. Start GPU box via launch.sh (or start existing stopped instance)
# 3. Wait for both, write chandra.ip
#
# Usage:
#   ./prod/scripts/spin_up.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$REPO_ROOT/vllm/aws/config.sh"

# 1. Start CPU box

CPU_ID=$(aws ec2 describe-instances \
    --region "$AWS_REGION" \
    --filters "Name=tag:Name,Values=ocr-bench-cpu-pipeline-01" \
              "Name=instance-state-name,Values=stopped" \
    --query "Reservations[0].Instances[0].InstanceId" \
    --output text 2>/dev/null || true)

if [[ -n "$CPU_ID" && "$CPU_ID" != "None" ]]; then
    echo "Starting CPU instance $CPU_ID..."
    aws ec2 start-instances --region "$AWS_REGION" --instance-ids "$CPU_ID" >/dev/null
    echo "Waiting for CPU instance..."
    aws ec2 wait instance-running --region "$AWS_REGION" --instance-ids "$CPU_ID"

    CPU_IP=$(aws ec2 describe-instances \
        --region "$AWS_REGION" --instance-ids "$CPU_ID" \
        --query "Reservations[0].Instances[0].PublicIpAddress" --output text)
    echo "CPU instance running at $CPU_IP (Elastic IP)"
else
    echo "No stopped CPU instance found. Launch one manually first."
fi

# 2. Start GPU box

GPU_ID_FILE="$INSTANCE_DIR/chandra.id"
GPU_REGION_FILE="$INSTANCE_DIR/chandra.region"

if [[ -f "$GPU_ID_FILE" ]]; then
    GPU_ID=$(cat "$GPU_ID_FILE")
    GPU_REGION=$(cat "$GPU_REGION_FILE" 2>/dev/null || echo "$AWS_REGION")

    STATE=$(aws ec2 describe-instances \
        --region "$GPU_REGION" --instance-ids "$GPU_ID" \
        --query "Reservations[0].Instances[0].State.Name" --output text 2>/dev/null || echo "terminated")

    if [[ "$STATE" == "stopped" ]]; then
        echo "Starting existing GPU instance $GPU_ID..."
        aws ec2 start-instances --region "$GPU_REGION" --instance-ids "$GPU_ID" >/dev/null
        aws ec2 wait instance-running --region "$GPU_REGION" --instance-ids "$GPU_ID"

        GPU_IP=$(aws ec2 describe-instances \
            --region "$GPU_REGION" --instance-ids "$GPU_ID" \
            --query "Reservations[0].Instances[0].PublicIpAddress" --output text)
        echo "$GPU_IP" > "$INSTANCE_DIR/chandra.ip"
        echo "GPU instance running at $GPU_IP"
    elif [[ "$STATE" == "running" ]]; then
        echo "GPU instance $GPU_ID already running"
    else
        echo "GPU instance $GPU_ID is $STATE. Launching fresh..."
        "$REPO_ROOT/vllm/aws/launch.sh" chandra
    fi
else
    echo "No tracked GPU instance. Launching fresh..."
    "$REPO_ROOT/vllm/aws/launch.sh" chandra
fi

echo ""
echo "Spin-up complete."
echo "  CPU: ${CPU_IP:-unknown}"
echo "  GPU: $(cat "$INSTANCE_DIR/chandra.ip" 2>/dev/null || echo 'unknown')"
echo ""

# Temporal + Postgres start via docker-compose on boot (systemd).
# Wait for health before declaring ready.
if [[ -n "$CPU_IP" && "$CPU_IP" != "None" ]]; then
    echo "Waiting for Temporal on CPU box..."
    for i in $(seq 1 24); do
        if ssh -o ConnectTimeout=5 "$CPU_IP" "docker compose -f /opt/ocr-benchmarking/docker-compose.yml exec -T temporal temporal operator cluster health" 2>/dev/null; then
            echo "Temporal is healthy."
            break
        fi
        sleep 5
    done
fi

echo ""
echo "Next steps:"
echo "  1. Verify Temporal UI: http://${CPU_IP:-localhost}:8233"
echo "  2. Verify chandra is serving: curl http://\$(cat $INSTANCE_DIR/chandra.ip):8004/health"
