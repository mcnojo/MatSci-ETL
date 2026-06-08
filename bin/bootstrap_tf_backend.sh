#!/usr/bin/env bash
# Create the S3 bucket + DynamoDB table that all terraform modules use for remote
# state and locking. Run once per AWS account. Safe to re-run — every operation
# is idempotent.
#
# Names + region must match infra/terraform/_backend.hcl. Change here AND there
# if `ocr-benchmarking-tfstate` is taken globally.

set -euo pipefail

BUCKET="${OCR_TF_STATE_BUCKET:-ocr-benchmarking-tfstate}"
TABLE="${OCR_TF_LOCK_TABLE:-ocr-benchmarking-tflock}"
REGION="${OCR_TF_STATE_REGION:-us-west-2}"

if ! command -v aws >/dev/null; then
  echo "error: aws CLI not on PATH" >&2; exit 1
fi
aws sts get-caller-identity --output text >/dev/null \
  || { echo "error: AWS creds not configured" >&2; exit 1; }

echo ">> bucket: $BUCKET (region $REGION)"
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "   exists"
else
  # us-east-1 rejects LocationConstraint; every other region requires it.
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=$REGION"
  fi
  echo "   created"
fi

aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'

echo ">> dynamodb table: $TABLE (region $REGION)"
if aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" >/dev/null 2>&1; then
  echo "   exists"
else
  aws dynamodb create-table --table-name "$TABLE" --region "$REGION" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST >/dev/null
  aws dynamodb wait table-exists --table-name "$TABLE" --region "$REGION"
  echo "   created"
fi

echo
echo "done. terraform modules can now init against the s3 backend."
