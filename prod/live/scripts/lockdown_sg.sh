#!/usr/bin/env bash
# Phase 4: Lock down the GPU security group so the vLLM/chandra port
# is only reachable from the CPU pipeline box, not the public internet.
#
# Usage:
#   ./prod/live/scripts/lockdown_sg.sh
#   ./prod/live/scripts/lockdown_sg.sh --dry-run
#
# Prerequisites:
#   - CPU and GPU instances already launched (via vllm/aws/launch.sh)
#   - AWS CLI configured with appropriate permissions

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$REPO_ROOT/vllm/aws/config.sh"

DRY_RUN="${1:-}"
GPU_PORTS=(8001 8002 8003 8004)  # all model ports

# Resolve security groups

GPU_SG_ID=$(aws ec2 describe-security-groups \
    --region "$AWS_REGION" \
    --group-names "$SECURITY_GROUP_NAME" \
    --query "SecurityGroups[0].GroupId" \
    --output text)

if [[ -z "$GPU_SG_ID" || "$GPU_SG_ID" == "None" ]]; then
    echo "ERROR: Security group '$SECURITY_GROUP_NAME' not found."
    echo "       Launch the GPU instance first: vllm/aws/launch.sh chandra"
    exit 1
fi

# CPU box uses the same SG in Phase 2 (single-host). In Phase 4 with
# separate hosts, create a dedicated CPU SG and update CPU_SG_ID here.
CPU_SG_ID="$GPU_SG_ID"

echo "GPU security group: $GPU_SG_ID"
echo "CPU security group: $CPU_SG_ID (source for allowed traffic)"
echo ""

# Remove public 0.0.0.0/0 rules on model ports

for PORT in "${GPU_PORTS[@]}"; do
    echo "Port $PORT:"

    # Check if a 0.0.0.0/0 rule exists for this port
    EXISTING=$(aws ec2 describe-security-groups \
        --region "$AWS_REGION" \
        --group-ids "$GPU_SG_ID" \
        --query "SecurityGroups[0].IpPermissions[?FromPort==\`$PORT\` && ToPort==\`$PORT\`].IpRanges[?CidrIp=='0.0.0.0/0']" \
        --output text)

    if [[ -n "$EXISTING" && "$EXISTING" != "None" ]]; then
        echo "  Revoking 0.0.0.0/0 ingress on port $PORT"
        if [[ "$DRY_RUN" != "--dry-run" ]]; then
            aws ec2 revoke-security-group-ingress \
                --region "$AWS_REGION" \
                --group-id "$GPU_SG_ID" \
                --protocol tcp --port "$PORT" --cidr 0.0.0.0/0
        else
            echo "  (dry-run: would revoke)"
        fi
    else
        echo "  No public rule found"
    fi

    # Add source-SG rule (CPU -> GPU on this port)
    echo "  Adding source-SG rule: $CPU_SG_ID -> port $PORT"
    if [[ "$DRY_RUN" != "--dry-run" ]]; then
        aws ec2 authorize-security-group-ingress \
            --region "$AWS_REGION" \
            --group-id "$GPU_SG_ID" \
            --protocol tcp --port "$PORT" \
            --source-group "$CPU_SG_ID" 2>/dev/null || echo "  (rule already exists)"
    else
        echo "  (dry-run: would add)"
    fi
    echo ""
done

echo "Done. GPU model ports are now only reachable from the CPU security group."
echo ""
echo "To verify:"
echo "  aws ec2 describe-security-groups --region $AWS_REGION --group-ids $GPU_SG_ID \\"
echo "    --query 'SecurityGroups[0].IpPermissions'"
