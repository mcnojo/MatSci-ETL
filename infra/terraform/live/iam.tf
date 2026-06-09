# Cross-module IAM grant: the SQS receive/delete perms + the live SSM read
# perm both attach to cpu-pipeline-01's role (owned by common/temporal). Tearing
# live/ down detaches the policy cleanly without touching the role itself.

data "aws_iam_policy_document" "consumer" {
  statement {
    sid = "PdfIngestionReceive"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
    ]
    resources = [aws_sqs_queue.pdf_ingestion.arn]
  }

  statement {
    sid     = "LiveSsmRead"
    actions = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = [
      "arn:${data.aws_partition.current.partition}:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.live_ssm_prefix}/*",
    ]
  }
}

resource "aws_iam_policy" "consumer" {
  name        = "${var.name_prefix}-live-consumer"
  description = "SQS receive + live SSM read for the ocr-ingestion consumer on cpu-pipeline-01."
  policy      = data.aws_iam_policy_document.consumer.json
}

resource "aws_iam_role_policy_attachment" "consumer" {
  role       = local.cpu_pipeline_role_name
  policy_arn = aws_iam_policy.consumer.arn
}
