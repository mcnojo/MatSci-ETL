#!/usr/bin/env bash
# One-shot EC2 stager: launches an ephemeral instance in-region, downloads
# (hf_id, revision) tuples from HuggingFace, syncs each to
# s3://<weights_bucket>/models/<id>/<rev>/, writes a .done sentinel LAST,
# self-terminates. Wrapper polls state, verifies sentinels afterwards.
#
#   bin/stage_model.sh <hf_id> [<revision>]           # revision defaults to "main"
#   bin/stage_model.sh --all [--force]                # every tuple in shared/vllm var.models
#   bin/stage_model.sh ... --instance-type <type>     # override compute (default m7i.xlarge)
#
# Requires: aws, jq, terraform. No local `hf` install — download runs on EC2.
# HF token: SSM param /ocr-bench/hf_token (fetched by the instance, not the laptop).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF="$REPO_ROOT/bin/tf.sh"
HF_TOKEN_SSM_PARAM="/ocr-bench/hf_token"
AWS_REGION="${AWS_REGION:-us-west-2}"
INSTANCE_TYPE_DEFAULT="m7i.xlarge"
ROOT_VOLUME_GB=200
POLL_INTERVAL_S=15

need() { command -v "$1" >/dev/null || { echo "error: $1 not on PATH" >&2; exit 1; }; }
need aws; need jq; need terraform

usage() {
  cat <<EOF
usage: bin/stage_model.sh <hf_id> [<revision>] [--force] [--instance-type <t>]
       bin/stage_model.sh --all [--force] [--instance-type <t>]

  <hf_id>          HF repo id, e.g. google/gemma-4-12b-it
  <revision>       S3 subdir (default "main"). Must match hf_revision in var.models.
  --all            Every (hf_id, hf_revision) tuple in var.models.
  --force          Re-stage even if .done exists.
  --instance-type  EC2 type for the stager (default $INSTANCE_TYPE_DEFAULT).
EOF
  exit 1
}

force=0; mode="single"; instance_type="$INSTANCE_TYPE_DEFAULT"
targets=()
[[ $# -eq 0 ]] && usage
while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)           mode="all"; shift ;;
    --force)         force=1; shift ;;
    --instance-type) [[ $# -ge 2 ]] || usage; instance_type="$2"; shift 2 ;;
    -h|--help)       usage ;;
    -*)              echo "error: unknown flag '$1'" >&2; usage ;;
    *)
      [[ $mode == "all" ]] && { echo "error: no positional args with --all" >&2; usage; }
      if [[ ${#targets[@]} -eq 0 ]]; then targets=("$1" "main")
      elif [[ ${#targets[@]} -eq 2 ]]; then targets[1]="$1"
      else echo "error: too many positional args" >&2; usage
      fi
      shift ;;
  esac
done

echo "==> reading shared/platform tfstate"
BUCKET=$("$TF" shared/platform output -raw vllm_weights_bucket 2>/dev/null || true)
PROFILE=$("$TF" shared/platform output -raw stager_instance_profile_name 2>/dev/null || true)
[[ -n "$BUCKET" && -n "$PROFILE" ]] || {
  echo "error: shared/platform outputs missing (vllm_weights_bucket, stager_instance_profile_name)." >&2
  echo "       run 'bin/tf.sh shared/platform init && bin/tf.sh shared/platform apply' first." >&2
  exit 1
}
echo "    bucket:  $BUCKET"
echo "    profile: $PROFILE"

# `terraform console` in shared/vllm returns a jsonencoded string; strip the
# outer quotes + unescape the interior "s to get the raw JSON array.
collect_tuples() {
  if [[ $mode == "all" ]]; then
    (cd "$REPO_ROOT/shared/vllm/terraform" && terraform console <<< \
      'jsonencode(flatten([for k, m in var.models : concat([{hf_id = m.hf_model_id, revision = m.hf_revision}], [for s in m.secondary_services : {hf_id = s.hf_model_id, revision = s.hf_revision}])]))' \
      2>/dev/null | tail -1 | sed -e 's/^"//' -e 's/"$//' -e 's/\\"/"/g')
  else
    jq -cn --arg id "${targets[0]}" --arg rev "${targets[1]}" '[{hf_id:$id, revision:$rev}]'
  fi
}

ALL_TUPLES=$(collect_tuples)
[[ -n "$ALL_TUPLES" && "$ALL_TUPLES" != "[]" ]] || {
  echo "error: no tuples to stage." >&2; exit 1
}

# Skip tuples already .done unless --force. Runs pre-launch so we don't spin
# up an instance just to no-op.
PENDING='[]'
n=$(echo "$ALL_TUPLES" | jq 'length')
for (( i=0; i<n; i++ )); do
  id=$(echo "$ALL_TUPLES" | jq -r ".[$i].hf_id")
  rev=$(echo "$ALL_TUPLES" | jq -r ".[$i].revision")
  if [[ $force -eq 0 ]] && aws s3 ls "s3://$BUCKET/models/$id/$rev/.done" >/dev/null 2>&1; then
    echo "    skip (.done present): $id @ $rev"
  else
    PENDING=$(echo "$PENDING" | jq --arg id "$id" --arg rev "$rev" '. + [{hf_id:$id, revision:$rev}]')
  fi
done
PENDING_COUNT=$(echo "$PENDING" | jq 'length')
if [[ "$PENDING_COUNT" -eq 0 ]]; then
  echo "==> nothing to stage. all requested tuples already .done."
  exit 0
fi

echo "==> $PENDING_COUNT tuple(s) to stage:"
echo "$PENDING" | jq -r '.[] | "      \(.hf_id) @ \(.revision)"'

echo "==> resolving latest AL2023 AMI"
AMI_ID=$(aws ssm get-parameter --region "$AWS_REGION" \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64 \
  --query 'Parameter.Value' --output text)
echo "    ami: $AMI_ID"

TUPLES_B64=$(echo "$PENDING" | base64 | tr -d '\n')

# user_data: install hf CLI, loop tuples, .done LAST, upload log to S3, poweroff.
# EOF is unquoted so wrapper-side vars ($BUCKET, $AWS_REGION, etc.) substitute
# now; EC2-side vars use \$ to survive to boot time.
USER_DATA_FILE=$(mktemp)
cat > "$USER_DATA_FILE" <<EOF
#!/bin/bash
set -uo pipefail
exec > >(tee -a /var/log/stage.log) 2>&1

TOKEN=\$(curl -s -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' http://169.254.169.254/latest/api/token)
INSTANCE_ID=\$(curl -s -H "X-aws-ec2-metadata-token: \$TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
LOG_S3="s3://$BUCKET/_stager_logs/\$(date -u +%Y%m%dT%H%M%SZ)-\$INSTANCE_ID.log"

cleanup() {
  local rc=\$?
  echo
  echo "==> exit \$rc; uploading log to \$LOG_S3"
  aws s3 cp /var/log/stage.log "\$LOG_S3" --only-show-errors || true
  echo "==> poweroff (self-terminate)"
  /sbin/poweroff
}
trap cleanup EXIT

echo "==> stager boot on \$INSTANCE_ID"
dnf install -y python3-pip jq
pip3 install --quiet huggingface_hub

# /tmp is tmpfs (~8 GB on m7i.xlarge); stage dirs + HF's internal buffers live
# on the 200 GB root volume instead. TMPDIR covers anything reading it.
STAGE_ROOT=/var/lib/stage
mkdir -p "\$STAGE_ROOT"
export TMPDIR="\$STAGE_ROOT/tmp"
mkdir -p "\$TMPDIR"

HF_TOKEN=\$(aws ssm get-parameter --region $AWS_REGION --name $HF_TOKEN_SSM_PARAM \
  --with-decryption --query Parameter.Value --output text)
export HF_TOKEN
# xet backend stalled from residential last time; disable on EC2 too — plain
# HTTP saturates the ENI just fine.
export HF_HUB_DISABLE_XET=1
export HF_HUB_DOWNLOAD_TIMEOUT=300

TUPLES=\$(echo '$TUPLES_B64' | base64 -d)
N=\$(echo "\$TUPLES" | jq 'length')

for i in \$(seq 0 \$((N-1))); do
  ID=\$(echo "\$TUPLES" | jq -r ".[\$i].hf_id")
  REV=\$(echo "\$TUPLES" | jq -r ".[\$i].revision")
  STAGE="\$STAGE_ROOT/\$i"
  DEST="s3://$BUCKET/models/\$ID/\$REV"

  echo
  echo "==> [\$((i+1))/\$N] \$ID @ \$REV"
  mkdir -p "\$STAGE"
  /usr/local/bin/hf download "\$ID" --revision "\$REV" --local-dir "\$STAGE"

  if find "\$STAGE" -type f -name '*.incomplete' -print -quit | grep -q .; then
    echo "error: .incomplete files remain in \$STAGE — refusing to publish" >&2
    exit 1
  fi
  [[ -s "\$STAGE/config.json" ]] || { echo "error: \$STAGE/config.json missing" >&2; exit 1; }

  echo "    aws s3 sync -> \$DEST/"
  aws s3 sync "\$STAGE/" "\$DEST/" --delete --only-show-errors

  # .done LAST — vLLM user_data checks it before sync, so torn uploads never boot.
  printf 'staged_at=%s\nhf_id=%s\nrevision=%s\ninstance=%s\n' \
    "\$(date -u +%Y-%m-%dT%H:%M:%SZ)" "\$ID" "\$REV" "\$INSTANCE_ID" \
    | aws s3 cp - "\$DEST/.done" --only-show-errors

  rm -rf "\$STAGE"
  echo "    done: \$DEST/"
done

echo
echo "==> all \$N tuples staged"
EOF

echo "==> launching stager (type=$instance_type, root=${ROOT_VOLUME_GB}GB gp3)"
INSTANCE_ID=$(aws ec2 run-instances \
  --region "$AWS_REGION" \
  --image-id "$AMI_ID" \
  --instance-type "$instance_type" \
  --iam-instance-profile "Name=$PROFILE" \
  --instance-initiated-shutdown-behavior terminate \
  --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=$ROOT_VOLUME_GB,VolumeType=gp3,DeleteOnTermination=true,Encrypted=true}" \
  --metadata-options "HttpTokens=required,HttpEndpoint=enabled,HttpPutResponseHopLimit=2" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=ocr-bench-stager},{Key=Purpose,Value=hf-to-s3}]" \
  --user-data "file://$USER_DATA_FILE" \
  --query 'Instances[0].InstanceId' --output text)
rm -f "$USER_DATA_FILE"

# Ctrl-C on the wrapper leaves the instance alive — it'll self-terminate on
# user_data completion regardless. Print a reminder rather than orphaning silently.
trap 'echo; echo "wrapper interrupted. instance $INSTANCE_ID will self-terminate on completion of user_data."; exit 130' INT

echo "    instance: $INSTANCE_ID"
echo
echo "    live tail (SSM registration takes ~30-60s post-boot):"
echo "      aws ssm start-session --region $AWS_REGION --target $INSTANCE_ID \\"
echo "        --document-name AWS-StartInteractiveCommand \\"
echo "        --parameters 'command=[\"sudo tail -F /var/log/stage.log\"]'"
echo
echo "==> polling every ${POLL_INTERVAL_S}s until terminated"

start=$(date +%s)
while :; do
  state=$(aws ec2 describe-instances --region "$AWS_REGION" --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo "unknown")
  elapsed=$(( $(date +%s) - start ))
  printf "    [%4ds] state=%s\n" "$elapsed" "$state"
  [[ "$state" == "terminated" ]] && break
  sleep "$POLL_INTERVAL_S"
done

echo
echo "==> verifying .done sentinels"
missing=0
for (( i=0; i<PENDING_COUNT; i++ )); do
  id=$(echo "$PENDING" | jq -r ".[$i].hf_id")
  rev=$(echo "$PENDING" | jq -r ".[$i].revision")
  if aws s3 ls "s3://$BUCKET/models/$id/$rev/.done" >/dev/null 2>&1; then
    echo "    ok:      $id @ $rev"
  else
    echo "    MISSING: $id @ $rev"
    missing=$((missing+1))
  fi
done

echo
if [[ $missing -eq 0 ]]; then
  echo "stage_model.sh: complete ($PENDING_COUNT tuples staged)."
else
  echo "stage_model.sh: FAILED ($missing/$PENDING_COUNT missing .done)."
  echo "  fetch stager log:"
  echo "    aws s3 ls s3://$BUCKET/_stager_logs/ | tail -1"
  echo "    aws s3 cp s3://$BUCKET/_stager_logs/<file>.log -"
  exit 1
fi
