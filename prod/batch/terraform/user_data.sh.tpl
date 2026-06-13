#!/bin/bash
# Batch worker bootstrap. Rendered by terraform's templatefile(); runs once at first boot.
# Variables substituted by terraform — see prod/batch/terraform/main.tf.

set -euo pipefail
exec > >(tee -a /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1

INSTALL_DIR=/opt/ocr-benchmarking
SECRETS_DIR=/etc/ocr-benchmarking
SECRETS_FILE="$SECRETS_DIR/tree_llm.env"
WORKER_LOG=/var/log/ocr-batch-worker.log

echo "[bootstrap] dnf install (worker_role=${worker_role})"
# Base system packages every role needs.
dnf -y update
dnf -y install \
    git \
    python3.11 \
    python3.11-devel \
    python3.11-pip \
    gcc \
    gcc-c++ \
    make \
    amazon-cloudwatch-agent

# Heavy CV stack and its system libs live behind --queues cpu/control. The
# GPU role registers only HTTP activities (no torch, no cv2, no doclayout) so
# skipping these saves ~3-4 GB on pip install and removes the libxcb/libGL
# requirement that previously crashed the GPU box in tmpfs.
if [ "${worker_role}" = "cpu" ]; then
    # libxcb + mesa-libGL: opencv-python (pulled transitively by camelot[cv]
    # and doclayout-yolo) loads libxcb.so.1 + libGL.so.1 at `import cv2` even
    # when invoked headlessly. AL2023 base AMI ships neither.
    dnf -y install libxcb mesa-libGL
fi

echo "[bootstrap] clone ${repo_url} @ ${repo_ref}"
git clone "${repo_url}" "$INSTALL_DIR"
git -C "$INSTALL_DIR" checkout "${repo_ref}"

echo "[bootstrap] venv + editable install (worker_role=${worker_role})"
python3.11 -m venv "$INSTALL_DIR/env"
"$INSTALL_DIR/env/bin/pip" install --upgrade pip
# Pin pip's scratch dir to disk-backed /var/tmp instead of AL2023's default
# /tmp (tmpfs at ~50% of RAM). On small-RAM Spot picks (c7i.large, c5.large
# = 4GB RAM → ~2GB tmpfs) the torch + nvidia-* wheels overflow tmpfs and
# pip dies with ENOSPC long before the 50GB EBS root is touched. /var/tmp
# on AL2023 is plain ext4/xfs on the root volume.
export TMPDIR=/var/tmp
# CPU role installs the pipeline-cpu extra (torch + cv2 + doclayout) since it
# registers ProcessPdfWorkflow + the CPU activities. GPU role installs bare
# base — it only ever runs HTTP activities (no workflow registration here).
if [ "${worker_role}" = "cpu" ]; then
    "$INSTALL_DIR/env/bin/pip" install -e "$INSTALL_DIR[pipeline-cpu]"
else
    "$INSTALL_DIR/env/bin/pip" install -e "$INSTALL_DIR"
fi

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

echo "[bootstrap] worker log file"
install -m 0644 /dev/null "$WORKER_LOG"

echo "[bootstrap] cloudwatch agent config"
# \$${aws:...} is CWAgent's runtime substitution: terraform templatefile()
# turns $$ into a literal $, then bash sees \$ and emits a literal $ — without
# the backslash, set -u aborts on $${aws:InstanceId} as an unbound variable.
# ${log_group_name} IS terraform's and gets substituted at plan time.
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<CWA_EOF
{
  "agent": { "metrics_collection_interval": 60, "run_as_user": "root" },
  "metrics": {
    "namespace": "OCR/Batch/Worker",
    "append_dimensions": {
      "InstanceId": "\$${aws:InstanceId}",
      "AutoScalingGroupName": "\$${aws:AutoScalingGroupName}"
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
          "pattern": "prod.batch.worker",
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
            "log_stream_name": "{instance_id}/ocr-batch-worker",
            "timezone": "UTC"
          },
          {
            "file_path": "/var/log/user-data.log",
            "log_group_name": "${log_group_name}",
            "log_stream_name": "{instance_id}/user-data",
            "timezone": "UTC"
          }
        ]
      }
    }
  }
}
CWA_EOF

systemctl enable --now amazon-cloudwatch-agent

echo "[bootstrap] systemd unit"
cat > /etc/systemd/system/ocr-batch-worker.service <<UNIT_EOF
[Unit]
Description=OCR batch worker (${worker_role} queue)
After=network-online.target amazon-cloudwatch-agent.service
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$SECRETS_FILE
Environment=PYTHONUNBUFFERED=1
Environment=AWS_REGION=${aws_region}
Environment=AWS_DEFAULT_REGION=${aws_region}
Environment=ARTIFACT_BUCKET=${artifact_bucket}
Environment=BATCH_LIFECYCLE_QUEUE=${lifecycle_queue}
Environment=OCR_VLLM_ENV_TAG=prod
Environment=OCR_VLLM_PREFER_PRIVATE_IP=1
# Cap torch/OMP/MKL threads — prevent N×vCPU oversubscription under max_concurrent.
Environment=OMP_NUM_THREADS=${torch_num_threads}
Environment=MKL_NUM_THREADS=${torch_num_threads}
Environment=TORCH_NUM_THREADS=${torch_num_threads}
Environment=OPENBLAS_NUM_THREADS=${torch_num_threads}
ExecStart=$INSTALL_DIR/env/bin/python -m prod.batch.worker \\
    --temporal-address ${temporal_address} \\
    --temporal-namespace ${temporal_namespace} \\
    --queues ${worker_role} \\
    --max-concurrent-cpu ${max_concurrent_cpu} \\
    --max-concurrent-gpu ${max_concurrent_gpu}
Restart=always
RestartSec=5
# Exceeds WORKER_GRACEFUL_SHUTDOWN_TIMEOUT (90s) so drain completes before SIGKILL.
TimeoutStopSec=120
KillSignal=SIGTERM
StandardOutput=append:$WORKER_LOG
StandardError=append:$WORKER_LOG

[Install]
WantedBy=multi-user.target
UNIT_EOF

chown ec2-user:ec2-user "$WORKER_LOG"

systemctl daemon-reload
systemctl enable --now ocr-batch-worker

echo "[bootstrap] complete"
