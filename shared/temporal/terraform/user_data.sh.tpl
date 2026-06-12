#!/bin/bash
# cpu-pipeline-01 bootstrap. Rendered by terraform's templatefile(); runs once at first boot.
# Variables surrounded by single-dollar braces are terraform substitutions;
# double-dollar braces escape to literal single-dollar braces for bash/CWAgent.

set -euo pipefail
exec > >(tee -a /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1

INSTALL_DIR=/opt/ocr-benchmarking
SECRETS_DIR=/etc/ocr-benchmarking
SECRETS_FILE="$SECRETS_DIR/tree_llm.env"
WORKER_LOG=/var/log/ocr-worker.log
INGESTION_LOG=/var/log/ocr-ingestion.log
DOCKER_COMPOSE_VERSION=v2.29.7

echo "[bootstrap] dnf install"
# libxcb + mesa-libGL: opencv-python (transitive dep via doclayout-yolo)
# requires libxcb.so.1 + libGL.so.1 even when used headlessly. AL2023 base
# AMI ships neither, so the import fails and the worker crash-loops without
# them. opencv-python-headless avoids the deps but pip resolves both wheels
# (doclayout-yolo pins regular) and binary collision on cv2/ is non-deterministic.
dnf -y update
dnf -y install \
    git \
    docker \
    python3.11 \
    python3.11-devel \
    python3.11-pip \
    gcc \
    gcc-c++ \
    make \
    amazon-cloudwatch-agent \
    libxcb \
    mesa-libGL

systemctl enable --now docker

echo "[bootstrap] docker compose plugin"
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL \
    "https://github.com/docker/compose/releases/download/$DOCKER_COMPOSE_VERSION/docker-compose-linux-x86_64" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

echo "[bootstrap] clone ${repo_url} @ ${repo_ref}"
git clone "${repo_url}" "$INSTALL_DIR"
git -C "$INSTALL_DIR" checkout "${repo_ref}"

echo "[bootstrap] venv + editable install"
python3.11 -m venv "$INSTALL_DIR/env"
"$INSTALL_DIR/env/bin/pip" install --upgrade pip
"$INSTALL_DIR/env/bin/pip" install -e "$INSTALL_DIR"

echo "[bootstrap] tree_llm key fetch"
mkdir -p "$SECRETS_DIR"
{
    val=$(aws ssm get-parameter \
        --region "${aws_region}" \
        --name "${tree_llm_ssm_prefix}/anthropic_api_key" \
        --with-decryption --query Parameter.Value --output text 2>/dev/null || true)
    echo "ANTHROPIC_API_KEY=$val"
    val=$(aws ssm get-parameter \
        --region "${aws_region}" \
        --name "${tree_llm_ssm_prefix}/openai_api_key" \
        --with-decryption --query Parameter.Value --output text 2>/dev/null || true)
    echo "OPENAI_API_KEY=$val"
} > "$SECRETS_FILE"
chmod 600 "$SECRETS_FILE"

echo "[bootstrap] log files"
install -m 0644 /dev/null "$WORKER_LOG"
install -m 0644 /dev/null "$INGESTION_LOG"

echo "[bootstrap] cloudwatch agent config"
# $${aws:...} is CWAgent's runtime substitution; ${log_group_name} IS terraform's
# and gets substituted at plan time.
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<CWA_EOF
{
  "agent": { "metrics_collection_interval": 60, "run_as_user": "root" },
  "metrics": {
    "namespace": "OCR/Live/Worker",
    "append_dimensions": {
      "InstanceId": "\$${aws:InstanceId}"
    },
    "aggregation_dimensions": [["InstanceId"]],
    "metrics_collected": {
      "cpu": {
        "measurement": ["cpu_usage_active"],
        "totalcpu": true,
        "metrics_collection_interval": 10
      },
      "mem": {
        "measurement": ["mem_used_percent"],
        "metrics_collection_interval": 10
      },
      "net": {
        "measurement": ["bytes_sent", "bytes_recv"],
        "metrics_collection_interval": 30
      },
      "procstat": [
        {
          "pattern": "prod.live.worker",
          "measurement": ["cpu_usage", "memory_rss"],
          "metrics_collection_interval": 10
        },
        {
          "pattern": "prod.live.ingestion.consumer",
          "measurement": ["cpu_usage", "memory_rss"],
          "metrics_collection_interval": 10
        }
      ]
    }
  },
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "$WORKER_LOG",
            "log_group_name": "${log_group_name}",
            "log_stream_name": "{instance_id}/ocr-worker",
            "timezone": "UTC"
          },
          {
            "file_path": "$INGESTION_LOG",
            "log_group_name": "${log_group_name}",
            "log_stream_name": "{instance_id}/ocr-ingestion",
            "timezone": "UTC"
          },
          {
            "file_path": "/var/log/user-data.log",
            "log_group_name": "${log_group_name}",
            "log_stream_name": "{instance_id}/user-data",
            "timezone": "UTC"
          },
          {
            "file_path": "/var/log/ocr-live-report.log",
            "log_group_name": "${log_group_name}",
            "log_stream_name": "{instance_id}/ocr-live-report",
            "timezone": "UTC"
          }
        ]
      }
    }
  }
}
CWA_EOF

systemctl enable --now amazon-cloudwatch-agent

echo "[bootstrap] systemd: ocr-temporal-stack"
cat > /etc/systemd/system/ocr-temporal-stack.service <<UNIT_EOF
[Unit]
Description=OCR Temporal stack (Postgres + Temporal + UI via docker compose)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=true
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/docker compose up -d --wait
ExecStop=/usr/bin/docker compose down

[Install]
WantedBy=multi-user.target
UNIT_EOF

echo "[bootstrap] systemd: ocr-worker"
cat > /etc/systemd/system/ocr-worker.service <<UNIT_EOF
[Unit]
Description=OCR live worker (cpu + gpu task queues)
After=ocr-temporal-stack.service
Requires=ocr-temporal-stack.service

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$SECRETS_FILE
Environment=PYTHONUNBUFFERED=1
Environment=AWS_REGION=${aws_region}
Environment=AWS_DEFAULT_REGION=${aws_region}
Environment=ARTIFACT_BUCKET=${artifact_bucket}
Environment=OCR_VLLM_ENV_TAG=prod
Environment=OCR_VLLM_PREFER_PRIVATE_IP=1
Environment=OMP_NUM_THREADS=${torch_num_threads}
Environment=MKL_NUM_THREADS=${torch_num_threads}
Environment=TORCH_NUM_THREADS=${torch_num_threads}
Environment=OPENBLAS_NUM_THREADS=${torch_num_threads}
ExecStart=$INSTALL_DIR/env/bin/python -m prod.live.worker \\
    --temporal-address localhost:7233 \\
    --temporal-namespace default \\
    --queues cpu,gpu \\
    --max-concurrent-cpu ${max_concurrent_cpu} \\
    --max-concurrent-gpu ${max_concurrent_gpu}
Restart=always
RestartSec=10
TimeoutStopSec=120
KillSignal=SIGTERM
StandardOutput=append:$WORKER_LOG
StandardError=append:$WORKER_LOG

[Install]
WantedBy=multi-user.target
UNIT_EOF

echo "[bootstrap] ocr-ingestion-env-fetch helper"
# Helper that ExecStartPre runs to pull the live SQS queue URL out of SSM.
# Returns non-zero when live/ hasn't been applied yet, so systemd retries
# every RestartSec until the parameter appears.
cat > /usr/local/bin/ocr-ingestion-env-fetch.sh <<'FETCH_EOF'
#!/bin/bash
set -euo pipefail
region="$1"; param="$2"; out="$3"
val=$(aws ssm get-parameter \
    --region "$region" \
    --name "$param" \
    --query Parameter.Value --output text 2>/dev/null)
[[ -n "$val" && "$val" != "None" ]] || { echo "ssm param $param empty/missing — has live/ been applied?" >&2; exit 1; }
install -m 0600 -D /dev/null "$out"
echo "OCR_LIVE_QUEUE_URL=$val" > "$out"
FETCH_EOF
chmod +x /usr/local/bin/ocr-ingestion-env-fetch.sh

echo "[bootstrap] systemd: ocr-ingestion"
cat > /etc/systemd/system/ocr-ingestion.service <<UNIT_EOF
[Unit]
Description=OCR live SQS-to-Temporal ingestion consumer
After=ocr-temporal-stack.service
Requires=ocr-temporal-stack.service

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$SECRETS_FILE
EnvironmentFile=$SECRETS_DIR/live.env
Environment=PYTHONUNBUFFERED=1
Environment=AWS_REGION=${aws_region}
Environment=AWS_DEFAULT_REGION=${aws_region}
Environment=ARTIFACT_BUCKET=${artifact_bucket}
Environment=OCR_VLLM_ENV_TAG=prod
Environment=OCR_VLLM_PREFER_PRIVATE_IP=1
ExecStartPre=/usr/local/bin/ocr-ingestion-env-fetch.sh ${aws_region} ${live_ssm_prefix}/queue_url $SECRETS_DIR/live.env
ExecStart=$INSTALL_DIR/env/bin/python -m prod.live.ingestion.consumer
Restart=always
RestartSec=10
StandardOutput=append:$INGESTION_LOG
StandardError=append:$INGESTION_LOG

[Install]
WantedBy=multi-user.target
UNIT_EOF

echo "[bootstrap] systemd: ocr-live-report.{service,timer} — daily 24h rolling-window live report"
# Cheap procedural report drop so there's always a recent live snapshot for
# `prod.reports compare`. Runs as root in the install dir; logs to the same
# CloudWatch log group as ocr-worker.
LIVE_REPORT_LOG=/var/log/ocr-live-report.log
install -m 0644 /dev/null "$LIVE_REPORT_LOG"

cat > /etc/systemd/system/ocr-live-report.service <<UNIT_EOF
[Unit]
Description=Build a 24h rolling-window live report
After=ocr-temporal-stack.service
Requires=ocr-temporal-stack.service

[Service]
Type=oneshot
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONUNBUFFERED=1
Environment=AWS_REGION=${aws_region}
Environment=AWS_DEFAULT_REGION=${aws_region}
ExecStart=$INSTALL_DIR/env/bin/python -m prod.reports \\
    --temporal-address localhost:7233 \\
    --temporal-namespace default \\
    live --since 24h --region ${aws_region}
StandardOutput=append:$LIVE_REPORT_LOG
StandardError=append:$LIVE_REPORT_LOG
UNIT_EOF

cat > /etc/systemd/system/ocr-live-report.timer <<UNIT_EOF
[Unit]
Description=Run ocr-live-report once a day

[Timer]
OnCalendar=daily
Persistent=true
AccuracySec=1m
Unit=ocr-live-report.service

[Install]
WantedBy=timers.target
UNIT_EOF

systemctl daemon-reload
systemctl enable --now ocr-temporal-stack
systemctl enable --now ocr-worker
# Enable ocr-ingestion; the ExecStartPre fails harmlessly until live/ is applied,
# then the unit comes up automatically on the next restart tick.
systemctl enable --now ocr-ingestion || true
systemctl enable --now ocr-live-report.timer

echo "[bootstrap] complete"
