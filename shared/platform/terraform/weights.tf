# Pre-staged vLLM weights bucket. Lives in shared/platform so it survives
# `bin/<motif>/down.sh`. Layout: s3://<bucket>/models/<hf_id>/<revision>/,
# with a `.done` sentinel written last by bin/stage_model.sh — user_data
# refuses to boot without it, so partial uploads never serve.

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "vllm_weights" {
  bucket = "ocr-bench-vllm-weights-${data.aws_caller_identity.current.account_id}" # account-scoped for global-namespace uniqueness

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "vllm_weights" {
  bucket = aws_s3_bucket.vllm_weights.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "vllm_weights" {
  bucket = aws_s3_bucket.vllm_weights.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "vllm_weights" {
  bucket                  = aws_s3_bucket.vllm_weights.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Reclaims parts leaked by a SIGKILL'd stager mid-upload.
resource "aws_s3_bucket_lifecycle_configuration" "vllm_weights" {
  bucket = aws_s3_bucket.vllm_weights.id

  rule {
    id     = "abort-stale-multipart"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}
