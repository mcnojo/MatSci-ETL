#!/usr/bin/env bash
# One-time setup for the CPU pipeline box (cpu-pipeline-01).
#
# Installs Docker, starts Temporal + Postgres via docker-compose,
# and creates a systemd unit for the worker.
#
# Usage:
#   ssh cpu-pipeline-01
#   cd /opt/ocr-benchmarking
#   ./prod/scripts/setup_cpu.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "=== Installing Docker ==="
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    echo "Docker installed. You may need to log out and back in for group changes."
fi

if ! command -v docker compose &>/dev/null; then
    echo "ERROR: docker compose plugin not found after install."
    exit 1
fi

echo "=== Starting Temporal infrastructure ==="
cd "$REPO_ROOT"
docker compose up -d
echo "Waiting for Temporal to be healthy..."
timeout 120 bash -c 'until docker compose exec temporal temporal operator cluster health 2>/dev/null; do sleep 5; done'
echo "Temporal is healthy."

echo "=== Creating systemd unit for worker ==="
WORKER_UNIT="/etc/systemd/system/ocr-worker.service"
sudo tee "$WORKER_UNIT" > /dev/null <<EOF
[Unit]
Description=OCR Pipeline Worker
After=docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=$REPO_ROOT
ExecStart=$REPO_ROOT/env/bin/python -m prod.worker --config prod/config/prod_config.yaml
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ocr-worker
echo "Worker unit created (not started — use 'sudo systemctl start ocr-worker')."

echo ""
echo "Setup complete. Next steps:"
echo "  1. docker compose ps  # verify Temporal + Postgres are up"
echo "  2. Visit http://localhost:8233 for Temporal UI"
echo "  3. sudo systemctl start ocr-worker"
