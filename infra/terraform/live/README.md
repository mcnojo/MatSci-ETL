# `live`

Live-motif infra: the SQS queue + S3-notification on `live/incoming/` + IAM
that the existing `prod/live/ingestion/consumer.py` depends on. The consumer
itself runs as a systemd unit on `cpu-pipeline-01` (managed by
`common/temporal`).

**Skeleton only — populated in Phase D of `AWS_DEPLOYMENT_PLAN.md`.** Until
then, the live path continues to work via the manually-provisioned SQS queue.
