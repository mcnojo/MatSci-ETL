#!/bin/bash
# vLLM box bootstrap. Rendered by templatefile(); runs once at first boot.
# Weights sync from S3 (bin/stage_model.sh) — never from HF Hub at runtime
# (prior gemma-4-12b download stalled at byte 7,875,958 from us-west-2).
# Weights land on the DLAMI's instance-store NVMe (multi-GB/s) rather than
# the root EBS (previously ~9 MB/s on chandra, causing a 20-min shard load).
# `services` = [{key, hf_model_id, hf_revision, port, ...}, ...]; chandra's
# box carries a bge-m3 embedding secondary alongside the primary OCR model.

set -euo pipefail
exec > >(tee -a /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1

VLLM_LOG=/var/log/vllm.log
SERVE_USER=ubuntu
# DLAMI mounts local instance-store NVMe here (~250 GB on g6/g6e.xlarge).
# Fail loud if it's absent — the root EBS path is 10-100x slower and would
# silently regress load times.
NVME_MOUNT=/opt/dlami/nvme
mountpoint -q "$NVME_MOUNT" || {
    echo "[bootstrap] FATAL: expected DLAMI NVMe mount at $NVME_MOUNT not present" >&2
    exit 1
}
MODELS_ROOT="$NVME_MOUNT/models"
WEIGHTS_BUCKET="${weights_bucket}"

echo "[bootstrap] pip install"
sudo -u "$SERVE_USER" -H bash -lc 'pip install --upgrade pip'
sudo -u "$SERVE_USER" -H bash -lc 'pip install "vllm>=0.24,<0.25" openai'

echo "[bootstrap] log file"
install -m 0644 -o "$SERVE_USER" -g "$SERVE_USER" /dev/null "$VLLM_LOG"

echo "[bootstrap] models root"
install -d -m 0755 -o "$SERVE_USER" -g "$SERVE_USER" "$MODELS_ROOT"

%{ for i, svc in services ~}
echo "[bootstrap] stage weights: ${svc.key} (${svc.hf_model_id}@${svc.hf_revision})"
# .done marker check first — its absence means a half-stage; refuse to serve.
SVC_${svc.key}_DIR="$MODELS_ROOT/${svc.key}"
install -d -m 0755 -o "$SERVE_USER" -g "$SERVE_USER" "$SVC_${svc.key}_DIR"

if ! sudo -u "$SERVE_USER" -H aws s3 ls \
     "s3://$WEIGHTS_BUCKET/models/${svc.hf_model_id}/${svc.hf_revision}/.done" >/dev/null 2>&1; then
    echo "[bootstrap] FATAL: no .done marker at s3://$WEIGHTS_BUCKET/models/${svc.hf_model_id}/${svc.hf_revision}/" >&2
    echo "[bootstrap]        run: bin/stage_model.sh ${svc.hf_model_id} ${svc.hf_revision}" >&2
    exit 1
fi

sudo -u "$SERVE_USER" -H aws s3 sync \
    "s3://$WEIGHTS_BUCKET/models/${svc.hf_model_id}/${svc.hf_revision}/" \
    "$SVC_${svc.key}_DIR/" \
    --exact-timestamps --only-show-errors

# config.json missing => sync landed on an empty prefix; don't boot.
[[ -s "$SVC_${svc.key}_DIR/config.json" ]] || {
    echo "[bootstrap] FATAL: $SVC_${svc.key}_DIR/config.json missing after sync" >&2
    exit 1
}

echo "[bootstrap] systemd: ocr-vllm-${svc.key} (${svc.hf_model_id} :${svc.port})"
# vllm lives in ubuntu's ~/.local/bin (DLAMI layout). Secondaries (i>0)
# serialize behind the primary via After=+Requires=+ExecStartPre curl loop
# so both units don't race torch.cuda.init on the shared device.
# HF_HUB_OFFLINE=1 => no silent Hub fallback on cache miss. --served-model-name
# keeps the OpenAI-API `model` field = HF ID even though we load from disk.
cat > /etc/systemd/system/ocr-vllm-${svc.key}.service <<UNIT_EOF
[Unit]
Description=vLLM serve ${svc.hf_model_id} (${svc.key})
After=network-online.target%{ if i > 0 } ocr-vllm-${services[0].key}.service%{ endif }
Wants=network-online.target
%{ if i > 0 ~}
Requires=ocr-vllm-${services[0].key}.service
%{ endif ~}

[Service]
Type=simple
User=$SERVE_USER
WorkingDirectory=/home/$SERVE_USER
Environment=HOME=/home/$SERVE_USER
Environment=PATH=/home/$SERVE_USER/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=HF_HUB_OFFLINE=1
%{ if i > 0 ~}
ExecStartPre=/bin/bash -c 'until curl -fsS --max-time 2 http://localhost:${services[0].port}/health >/dev/null 2>&1; do sleep 5; done'
%{ endif ~}
ExecStart=/home/$SERVE_USER/.local/bin/vllm serve $SVC_${svc.key}_DIR \\
    --served-model-name ${svc.hf_model_id} \\
    --port ${svc.port} \\
    --tensor-parallel-size 1 \\
    --gpu-memory-utilization ${svc.gpu_memory_utilization} \\
    --trust-remote-code \\
    --max-model-len ${svc.max_model_len} \\
    ${svc.extra_args}
Restart=on-failure
RestartSec=30
StandardOutput=append:$VLLM_LOG
StandardError=append:$VLLM_LOG

[Install]
WantedBy=multi-user.target
UNIT_EOF
systemctl daemon-reload
systemctl enable --now ocr-vllm-${svc.key}
%{ endfor ~}

echo "[bootstrap] nvidia-smi -> CloudWatch sidecar (${gpu_metrics_namespace})"
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
# Per-line: util,memused_mib,memtotal_mib. One device per row — single-GPU
# instance types (g6.xlarge / g6e.xlarge) emit exactly one row.
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

echo "[bootstrap] complete (vLLM starting from local weights — tail $VLLM_LOG)"
