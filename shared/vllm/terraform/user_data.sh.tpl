#!/bin/bash
# vLLM box bootstrap. Rendered by terraform's templatefile(); runs once at first boot.
# Installs two vLLM systemd units on one GPU box:
#   - ocr-vllm-vision-serve   :${vllm_port}            ${hf_model_id}
#   - ocr-vllm-tree-llm-serve :${tree_llm_port}        ${tree_llm_hf_model_id}
# Each process claims its own slice of GPU memory via --gpu-memory-utilization,
# so vision + tree_llm + headroom must sum to <= 1.0 (see variables.tf).

set -euo pipefail
exec > >(tee -a /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1

VISION_LOG=/var/log/vllm_vision.log
TREE_LLM_LOG=/var/log/vllm_tree_llm.log
SERVE_USER=ubuntu

echo "[bootstrap] pip install"
sudo -u "$SERVE_USER" -H bash -lc 'pip install --upgrade pip'
sudo -u "$SERVE_USER" -H bash -lc 'pip install vllm openai'

echo "[bootstrap] log files"
install -m 0644 -o "$SERVE_USER" -g "$SERVE_USER" /dev/null "$VISION_LOG"
install -m 0644 -o "$SERVE_USER" -g "$SERVE_USER" /dev/null "$TREE_LLM_LOG"

echo "[bootstrap] systemd: ocr-vllm-vision-serve"
# `vllm` lives in the serve user's ~/.local/bin per the DLAMI Python layout.
cat > /etc/systemd/system/ocr-vllm-vision-serve.service <<UNIT_EOF
[Unit]
Description=vLLM serve (vision) ${hf_model_id}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVE_USER
WorkingDirectory=/home/$SERVE_USER
Environment=HOME=/home/$SERVE_USER
Environment=PATH=/home/$SERVE_USER/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/$SERVE_USER/.local/bin/vllm serve ${hf_model_id} \\
    --port ${vllm_port} \\
    --tensor-parallel-size 1 \\
    --gpu-memory-utilization ${vision_gpu_memory_utilization} \\
    --trust-remote-code \\
    --max-model-len ${vision_max_model_len} \\
    ${vllm_extra_args}
Restart=on-failure
RestartSec=30
StandardOutput=append:$VISION_LOG
StandardError=append:$VISION_LOG

[Install]
WantedBy=multi-user.target
UNIT_EOF

echo "[bootstrap] systemd: ocr-vllm-tree-llm-serve"
cat > /etc/systemd/system/ocr-vllm-tree-llm-serve.service <<UNIT_EOF
[Unit]
Description=vLLM serve (tree_llm) ${tree_llm_hf_model_id}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVE_USER
WorkingDirectory=/home/$SERVE_USER
Environment=HOME=/home/$SERVE_USER
Environment=PATH=/home/$SERVE_USER/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/$SERVE_USER/.local/bin/vllm serve ${tree_llm_hf_model_id} \\
    --port ${tree_llm_port} \\
    --tensor-parallel-size 1 \\
    --gpu-memory-utilization ${tree_llm_gpu_memory_utilization} \\
    --trust-remote-code \\
    --max-model-len ${tree_llm_max_model_len} \\
    ${tree_llm_extra_args}
Restart=on-failure
RestartSec=30
StandardOutput=append:$TREE_LLM_LOG
StandardError=append:$TREE_LLM_LOG

[Install]
WantedBy=multi-user.target
UNIT_EOF

systemctl daemon-reload
systemctl enable --now ocr-vllm-vision-serve
systemctl enable --now ocr-vllm-tree-llm-serve

echo "[bootstrap] nvidia-smi → CloudWatch sidecar (${gpu_metrics_namespace})"
# Polls nvidia-smi, publishes one MetricDatum per metric tagged with InstanceId.
# IAM grant lives in vllm.tf::aws_iam_policy_document.vllm (cloudwatch:PutMetricData).
cat > /usr/local/bin/ocr-gpu-metrics.sh <<'GPU_EOF'
#!/bin/bash
set -euo pipefail
NAMESPACE="$1"; REGION="$2"
INSTANCE_ID=$(curl -fsSL -H "X-aws-ec2-metadata-token: $(curl -fsSL -X PUT \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
    http://169.254.169.254/latest/api/token)" \
    http://169.254.169.254/latest/meta-data/instance-id)
# Per-line: util,memused_mib,memtotal_mib. One device per row — sum across rows
# if multi-GPU is later added; today a single row is the norm for g6.xlarge.
while IFS=, read -r util mem_used mem_total; do
    util=$(echo "$util" | tr -d ' %')
    mem_used=$(echo "$mem_used" | tr -d ' MiB')
    mem_total=$(echo "$mem_total" | tr -d ' MiB')
    aws cloudwatch put-metric-data --region "$REGION" --namespace "$NAMESPACE" \
        --metric-data \
        "MetricName=gpu_utilization_percent,Unit=Percent,Value=$util,Dimensions=[{Name=InstanceId,Value=$INSTANCE_ID}]" \
        "MetricName=gpu_memory_used_mib,Unit=Megabytes,Value=$mem_used,Dimensions=[{Name=InstanceId,Value=$INSTANCE_ID}]" \
        "MetricName=gpu_memory_total_mib,Unit=Megabytes,Value=$mem_total,Dimensions=[{Name=InstanceId,Value=$INSTANCE_ID}]"
done < <(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader)
GPU_EOF
chmod +x /usr/local/bin/ocr-gpu-metrics.sh

cat > /etc/systemd/system/ocr-gpu-metrics.service <<UNIT_EOF
[Unit]
Description=Publish nvidia-smi GPU metrics to CloudWatch (${gpu_metrics_namespace})
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/ocr-gpu-metrics.sh ${gpu_metrics_namespace} ${aws_region}
UNIT_EOF

cat > /etc/systemd/system/ocr-gpu-metrics.timer <<UNIT_EOF
[Unit]
Description=Run ocr-gpu-metrics every ${gpu_metrics_interval}s

[Timer]
OnBootSec=60s
OnUnitActiveSec=${gpu_metrics_interval}s
AccuracySec=5s
Unit=ocr-gpu-metrics.service

[Install]
WantedBy=timers.target
UNIT_EOF

systemctl daemon-reload
systemctl enable --now ocr-gpu-metrics.timer

echo "[bootstrap] complete (models still downloading — tail $VISION_LOG and $TREE_LLM_LOG)"
