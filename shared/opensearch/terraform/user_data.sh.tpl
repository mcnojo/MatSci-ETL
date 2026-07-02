#!/usr/bin/env bash
# Single-node OpenSearch on Docker. Amazon Linux 2023 ARM64.
#
# Data lives on the root EBS at /var/lib/opensearch — persists across reboots.
# Snapshots to s3://${snapshot_bucket} are configured after boot via a one-shot
# unit; operators can also register the repo manually.

set -euxo pipefail
exec > >(tee -a /var/log/user_data.log) 2>&1

dnf -y update
dnf -y install docker jq awscli
systemctl enable --now docker

# Kernel tunings OpenSearch expects
sysctl -w vm.max_map_count=262144
echo "vm.max_map_count=262144" > /etc/sysctl.d/99-opensearch.conf

# Directories owned by the OS container user (uid 1000)
mkdir -p /var/lib/opensearch /var/log/opensearch
chown -R 1000:1000 /var/lib/opensearch /var/log/opensearch

ADMIN_PASSWORD="$(aws ssm get-parameter \
  --name "${admin_password_param}" \
  --with-decryption \
  --region "${aws_region}" \
  --query 'Parameter.Value' --output text)"

# Compose file is small; write inline. The `plugins.security.disabled=false`
# default keeps basic auth on. Bind 0.0.0.0 — the SG is the perimeter.
cat >/etc/opensearch/docker-compose.yml <<EOF
services:
  opensearch:
    image: opensearchproject/opensearch:${opensearch_version}
    container_name: opensearch
    restart: unless-stopped
    ulimits:
      memlock: { soft: -1, hard: -1 }
      nofile: { soft: 65536, hard: 65536 }
    environment:
      - discovery.type=single-node
      - bootstrap.memory_lock=true
      - OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g
      - OPENSEARCH_INITIAL_ADMIN_PASSWORD=$${ADMIN_PASSWORD}
      - plugins.security.ssl.http.enabled=true
      - cluster.name=chem-lit
      - node.name=os-01
    volumes:
      - /var/lib/opensearch:/usr/share/opensearch/data
      - /var/log/opensearch:/usr/share/opensearch/logs
    ports:
      - "9200:9200"
EOF

mkdir -p /etc/opensearch
cat >/etc/opensearch/.env <<EOF
ADMIN_PASSWORD=$${ADMIN_PASSWORD}
EOF

# Bring it up
cd /etc/opensearch
docker compose --env-file /etc/opensearch/.env up -d

# One-shot snapshot repo registration. Runs after OS is responsive; retries
# for up to 5 minutes to cover cold-start warmup.
cat >/usr/local/bin/register-snapshot-repo.sh <<'BASH'
#!/usr/bin/env bash
set -euxo pipefail
PASS="$(grep '^ADMIN_PASSWORD' /etc/opensearch/.env | cut -d= -f2)"
for i in $(seq 1 30); do
  if curl -sk -u "admin:$${PASS}" https://localhost:9200 >/dev/null; then
    break
  fi
  sleep 10
done
curl -sk -u "admin:$${PASS}" -H 'Content-Type: application/json' \
  -X PUT 'https://localhost:9200/_snapshot/s3-primary' \
  -d "{
    \"type\": \"s3\",
    \"settings\": {
      \"bucket\": \"${snapshot_bucket}\",
      \"region\": \"${aws_region}\",
      \"base_path\": \"snapshots\"
    }
  }"
BASH
chmod +x /usr/local/bin/register-snapshot-repo.sh

# Fire the registration; failures logged but don't block boot.
nohup /usr/local/bin/register-snapshot-repo.sh > /var/log/register-snapshot.log 2>&1 &

echo "user_data complete"
