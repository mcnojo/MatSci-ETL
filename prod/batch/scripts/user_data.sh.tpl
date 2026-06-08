#!/bin/bash
# Batch worker bootstrap. Rendered by terraform's templatefile(); runs once at first boot.
# Variables substituted by terraform — see infra/terraform/batch_fleet/main.tf.

set -euo pipefail
exec > >(tee -a /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1

INSTALL_DIR=/opt/ocr-benchmarking
WORKER_LOG=/var/log/ocr-batch-worker.log

echo "[bootstrap] dnf install"
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

echo "[bootstrap] clone ${repo_url} @ ${repo_ref}"
git clone "${repo_url}" "$INSTALL_DIR"
git -C "$INSTALL_DIR" checkout "${repo_ref}"

echo "[bootstrap] venv + editable install"
python3.11 -m venv "$INSTALL_DIR/env"
"$INSTALL_DIR/env/bin/pip" install --upgrade pip
"$INSTALL_DIR/env/bin/pip" install -e "$INSTALL_DIR"

echo "[bootstrap] worker log file"
install -m 0644 /dev/null "$WORKER_LOG"

echo "[bootstrap] cloudwatch agent config"
# $${aws:...} is CWAgent's runtime substitution; doubling the $ escapes it
# from terraform's templatefile(), which writes a literal $. ${log_group_name}
# IS terraform's and gets substituted at plan time.
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<CWA_EOF
{
  "agent": { "metrics_collection_interval": 60, "run_as_user": "root" },
  "metrics": {
    "namespace": "OCR/Batch/Worker",
    "append_dimensions": {
      "InstanceId": "$${aws:InstanceId}",
      "AutoScalingGroupName": "$${aws:AutoScalingGroupName}"
    },
    "aggregation_dimensions": [["InstanceId"]],
    "metrics_collected": {
      "cpu": {
        "measurement": ["cpu_usage_idle"],
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
Environment=PYTHONUNBUFFERED=1
Environment=AWS_REGION=${aws_region}
Environment=AWS_DEFAULT_REGION=${aws_region}
Environment=ARTIFACT_BUCKET=${artifact_bucket}
Environment=BATCH_LIFECYCLE_QUEUE=${lifecycle_queue}
# Cap torch/OMP/MKL threads — prevent N×vCPU oversubscription under max_concurrent.
Environment=OMP_NUM_THREADS=${torch_num_threads}
Environment=MKL_NUM_THREADS=${torch_num_threads}
Environment=TORCH_NUM_THREADS=${torch_num_threads}
Environment=OPENBLAS_NUM_THREADS=${torch_num_threads}
ExecStart=$INSTALL_DIR/env/bin/python -m prod.live.worker \\
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
