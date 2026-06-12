# `prod/live/terraform`

Live-motif infra. Self-contained for everything live-specific; consumes
`shared/temporal` via `terraform_remote_state` for the cross-cutting box
identity (artifact bucket name, cpu-pipeline-01's IAM role name, the live
SSM prefix).

| Resource                              | Purpose                                                       |
| ------------------------------------- | ------------------------------------------------------------- |
| `aws_sqs_queue.pdf_ingestion`         | PDFs land in S3 → notification → this queue → ocr-ingestion   |
| `aws_sqs_queue_policy`                | Authorizes `s3.amazonaws.com` to publish from the bucket only |
| `aws_s3_bucket_notification`          | `s3:ObjectCreated:*` on `live/incoming/*.pdf` → SQS           |
| `aws_ssm_parameter.queue_url`         | Queue URL handoff to cpu-pipeline-01 (fetched at unit start)  |
| `aws_iam_policy.consumer` + attach    | Cross-module attach onto cpu-pipeline-01's role               |

Apply order: `shared/temporal` first, then this module.

## S3 bucket notification — single-resource constraint

The AWS provider permits exactly one `aws_s3_bucket_notification` per bucket.
`prod/batch/terraform/` owns its own notification resource on
`batches/incoming/<id>/manifest.json`, so this module and `prod/batch/`
cannot be applied against the same bucket simultaneously. The operator UX
(`bin/<motif>/{up,down}.sh`) is one motif at a time — `down.sh` clears the
notification before another motif's `up.sh` recreates it.
