#!/bin/bash
# vLLM box bootstrap. Rendered by terraform's templatefile(); runs once at first boot.
# Mirrors vllm/aws/setup_remote.sh but wires up a proper systemd unit instead of nohup.

set -euo pipefail
exec > >(tee -a /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1

VLLM_LOG=/var/log/vllm_serve.log
SERVE_USER=ubuntu

echo "[bootstrap] pip install"
sudo -u "$SERVE_USER" -H bash -lc 'pip install --upgrade pip'
sudo -u "$SERVE_USER" -H bash -lc 'pip install vllm openai'

echo "[bootstrap] log file"
install -m 0644 -o "$SERVE_USER" -g "$SERVE_USER" /dev/null "$VLLM_LOG"

echo "[bootstrap] systemd: ocr-vllm-serve"
# `vllm` lives in the serve user's ~/.local/bin per the DLAMI Python layout.
cat > /etc/systemd/system/ocr-vllm-serve.service <<UNIT_EOF
[Unit]
Description=vLLM serve ${hf_model_id}
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
    --gpu-memory-utilization 0.9 \\
    --trust-remote-code \\
    --max-model-len 8192 \\
    ${vllm_extra_args}
Restart=on-failure
RestartSec=30
StandardOutput=append:$VLLM_LOG
StandardError=append:$VLLM_LOG

[Install]
WantedBy=multi-user.target
UNIT_EOF

systemctl daemon-reload
systemctl enable --now ocr-vllm-serve

echo "[bootstrap] complete (model still downloading — tail $VLLM_LOG)"
