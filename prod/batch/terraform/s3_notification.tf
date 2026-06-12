# S3 → batch_trigger Lambda notification.
#
# IMPORTANT: aws_s3_bucket_notification is bucket-scoped — terraform allows
# exactly ONE resource per bucket. The live motif owns its own notification
# (prod/live/terraform/s3_notification.tf), so batch and live cannot be
# simultaneously applied against the same bucket. The operator UX
# (bin/<motif>/{up,down}.sh) is one-motif-at-a-time, so this matches the
# operator model: bin/<motif>/down.sh clears the notification before another
# motif's up.sh recreates it.

resource "aws_lambda_permission" "batch_trigger_from_s3" {
  statement_id  = "${var.name_prefix}-trigger-allow-s3-invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.batch_trigger.arn
  principal     = "s3.amazonaws.com"
  source_arn    = local.bucket_arn
}

resource "aws_s3_bucket_notification" "artifacts" {
  bucket = local.artifact_bucket

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
