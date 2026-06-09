# `live`

Live-motif infra. Provisions:

| Resource                              | Purpose                                                       |
| ------------------------------------- | ------------------------------------------------------------- |
| `aws_sqs_queue.pdf_ingestion`         | PDFs land in S3 → notification → this queue → ocr-ingestion   |
| `aws_sqs_queue_policy`                | Authorizes `s3.amazonaws.com` to publish from the bucket only |
| `aws_s3_bucket_notification`          | `s3:ObjectCreated:*` on `live/incoming/*.pdf` → SQS           |
| `aws_ssm_parameter.queue_url`         | Queue URL handoff to cpu-pipeline-01 (fetched at unit start)  |
| `aws_iam_policy.consumer` + attach    | Cross-module attach onto cpu-pipeline-01's role               |

Reads `common/temporal` via `terraform_remote_state` for the artifact bucket
name and cpu-pipeline-01's role ARN.

## S3 bucket notification — single-resource constraint

The AWS provider permits exactly one `aws_s3_bucket_notification` per bucket.
`batch/` owns its own notification resource on `batches/incoming/<id>/manifest.json`,
so this module and `batch/` cannot be applied against the same bucket
simultaneously. The operator UX (`bin/<motif>/{up,down}.sh`) is one motif at
a time — `down.sh` clears the notification before another motif's `up.sh`
recreates it.
