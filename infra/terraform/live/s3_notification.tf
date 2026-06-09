# Live ingestion notification on the artifact bucket.
#
# IMPORTANT: aws_s3_bucket_notification is bucket-scoped — terraform allows
# exactly ONE resource per bucket. The batch motif owns its own notification
# (batch/s3_notification.tf), so live and batch cannot be simultaneously
# applied against the same bucket. Per AWS_DEPLOYMENT_PLAN.md the operator
# UX is one-motif-at-a-time, so this constraint matches the operator model:
# bin/{motif}/down.sh tears the notification down before bin/{other}/up.sh
# stands a new one up.

resource "aws_s3_bucket_notification" "artifacts" {
  bucket = local.artifact_bucket

  queue {
    queue_arn     = aws_sqs_queue.pdf_ingestion.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = var.incoming_prefix
    filter_suffix = var.pdf_suffix
  }

  # SQS requires the queue policy to authorize s3.amazonaws.com BEFORE the
  # notification is registered, otherwise PutBucketNotification fails.
  depends_on = [aws_sqs_queue_policy.pdf_ingestion]
}
