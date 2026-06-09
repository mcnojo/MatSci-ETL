# Phase D — S3 → batch_trigger Lambda notification.
#
# IMPORTANT: aws_s3_bucket_notification is bucket-scoped: terraform allows
# exactly ONE resource per bucket. If Phase E (live ingestion) adds its own
# Lambda notification on a different prefix (e.g. `live/incoming/`), the two
# must be coordinated — likely by moving the notification block into
# common/temporal (which owns the bucket) and pulling Lambda ARNs from
# batch/ + live/ via terraform_remote_state. Until then, this module owns
# the notification config and any other consumers must merge here.

resource "aws_lambda_permission" "batch_trigger_from_s3" {
  statement_id  = "${var.name_prefix}-trigger-allow-s3-invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.batch_trigger.arn
  principal     = "s3.amazonaws.com"
  source_arn    = "arn:${data.aws_partition.current.partition}:s3:::${var.artifact_bucket}"
}

resource "aws_s3_bucket_notification" "artifacts" {
  bucket = var.artifact_bucket

  lambda_function {
    lambda_function_arn = aws_lambda_function.batch_trigger.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = var.incoming_prefix
    filter_suffix       = ".json"
  }

  # S3 requires the invoke permission to exist BEFORE the notification is
  # registered, otherwise PutBucketNotification fails.
  depends_on = [aws_lambda_permission.batch_trigger_from_s3]
}
