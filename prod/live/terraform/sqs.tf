# The PDF ingestion queue + its policy allowing the artifact bucket's S3
# notification to publish into it. SourceAccount/SourceArn scope the
# permission to this account's bucket, preventing cross-account misuse.

resource "aws_sqs_queue" "pdf_ingestion" {
  name                       = "${var.name_prefix}-pdf-ingestion"
  visibility_timeout_seconds = var.queue_visibility_timeout_s
  message_retention_seconds  = var.queue_message_retention_s
}

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "pdf_ingestion_from_s3" {
  statement {
    sid     = "S3SendMessage"
    actions = ["sqs:SendMessage"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    resources = [aws_sqs_queue.pdf_ingestion.arn]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [local.bucket_arn]
    }
  }
}

resource "aws_sqs_queue_policy" "pdf_ingestion" {
  queue_url = aws_sqs_queue.pdf_ingestion.id
  policy    = data.aws_iam_policy_document.pdf_ingestion_from_s3.json
}
