# `prod/live/terraform`

Live-motif infra. Reads `shared/temporal` (artifact bucket, cpu-pipeline-01's
role + SG, live SSM prefix) and `shared/vllm` (per-model SG + port) via
`terraform_remote_state`.

| Resource                              | Purpose                                                       |
| ------------------------------------- | ------------------------------------------------------------- |
| `aws_sqs_queue.pdf_ingestion`         | S3 PUT -> notification -> queue -> ocr-ingestion consumer     |
| `aws_sqs_queue_policy`                | Authorizes `s3.amazonaws.com` from this account's bucket only |
| `aws_s3_bucket_notification`          | `s3:ObjectCreated:*` on `live/incoming/*.pdf` -> SQS          |
| `aws_ssm_parameter.queue_url`         | Queue URL handoff; ocr-ingestion fetches at unit start        |
| `aws_iam_policy.consumer` + attach    | SQS recv/delete + live SSM read, attached to cpu-pipeline role |
| `aws_security_group_rule.vllm_from_cpu_pipeline` | Per-model ingress cpu-pipeline-01 -> vLLM private IP |

Apply order: `shared/temporal` and `shared/vllm` first, then this module.

## Single-notification constraint

AWS allows exactly one `aws_s3_bucket_notification` per bucket. `prod/batch/`
owns its own notification on `batches/incoming/<id>/manifest.json`, so live and
batch cannot be applied against the same bucket simultaneously. Operator UX
(`bin/<motif>/{up,down}.sh`) is one motif at a time; `down.sh` clears the
notification before the other motif's `up.sh` recreates it.
