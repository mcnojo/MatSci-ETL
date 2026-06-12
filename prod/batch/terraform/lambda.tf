# batch_trigger Lambda.
#
# Fires on S3 PUT of <bucket>/{incoming_prefix}<batch_id>/manifest.json,
# validates the manifest, starts a BatchRunWorkflow keyed by batch_id with
# REJECT_DUPLICATE. The workflow itself owns batch lifecycle — this Lambda
# is just the trigger.
#
# Bundle: prod/batch/lambdas/batch_trigger/build.sh prepares build/bundle/;
# the archive_file data source below zips it at plan time. Re-run build.sh
# and `terraform apply` whenever the handler or bundled configs change.

locals {
  lambda_function_name = "${var.name_prefix}-trigger"
  lambda_bundle_dir    = "${path.module}/../lambdas/batch_trigger/build/bundle"
  lambda_zip_path      = "${path.module}/../lambdas/batch_trigger/build/lambda.zip"
}

data "archive_file" "batch_trigger" {
  type        = "zip"
  source_dir  = local.lambda_bundle_dir
  output_path = local.lambda_zip_path
}

# Lambda-only SG, egress to anywhere; ingress isn't needed (Lambda doesn't
# accept inbound traffic, even in VPC).
resource "aws_security_group" "batch_trigger_lambda" {
  name        = "${var.name_prefix}-trigger-lambda-sg"
  description = "Batch trigger Lambda. Egress only; Temporal access is granted via cross-SG ingress on cpu-pipeline-01."
  vpc_id      = data.aws_vpc.selected.id

  egress {
    description      = "all egress"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
}

# Symmetric to temporal_from_workers in main.tf — out-of-module SG, scoped
# destroy reverts cleanly.
resource "aws_security_group_rule" "temporal_from_trigger_lambda" {
  description              = "Batch trigger Lambda to Temporal on cpu-pipeline-01"
  type                     = "ingress"
  from_port                = 7233
  to_port                  = 7233
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.batch_trigger_lambda.id
  security_group_id        = local.cpu_pipeline_security_group_id
}

# IAM
data "aws_iam_policy_document" "batch_trigger_lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "batch_trigger_lambda" {
  name               = "${var.name_prefix}-trigger-lambda"
  assume_role_policy = data.aws_iam_policy_document.batch_trigger_lambda_assume.json
}

# VPC ENI lifecycle (CreateNetworkInterface / Describe / Delete) for VPC-attached Lambdas.
resource "aws_iam_role_policy_attachment" "batch_trigger_lambda_vpc" {
  role       = aws_iam_role.batch_trigger_lambda.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "batch_trigger_lambda" {
  statement {
    sid     = "IncomingPrefixRead"
    actions = ["s3:GetObject"]
    resources = [
      "${local.bucket_arn}/${var.incoming_prefix}*",
    ]
  }

  statement {
    sid       = "DlqSend"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.batch_trigger_dlq.arn]
  }

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.batch_trigger_lambda.arn}:*"]
  }
}

resource "aws_iam_policy" "batch_trigger_lambda" {
  name   = "${var.name_prefix}-trigger-lambda"
  policy = data.aws_iam_policy_document.batch_trigger_lambda.json
}

resource "aws_iam_role_policy_attachment" "batch_trigger_lambda" {
  role       = aws_iam_role.batch_trigger_lambda.name
  policy_arn = aws_iam_policy.batch_trigger_lambda.arn
}

# Pre-created so the IAM grant is resource-scopable (vs an open `*`).
resource "aws_cloudwatch_log_group" "batch_trigger_lambda" {
  name              = "/aws/lambda/${local.lambda_function_name}"
  retention_in_days = 14
}

resource "aws_sqs_queue" "batch_trigger_dlq" {
  name                      = "${var.name_prefix}-trigger-dlq"
  message_retention_seconds = 1209600 # 14 days — plan §D requirement
}

resource "aws_lambda_function" "batch_trigger" {
  function_name = local.lambda_function_name
  role          = aws_iam_role.batch_trigger_lambda.arn

  filename         = data.archive_file.batch_trigger.output_path
  source_code_hash = data.archive_file.batch_trigger.output_base64sha256

  runtime       = var.lambda_runtime
  architectures = [var.lambda_architecture]
  handler       = "handler.handler"
  memory_size   = var.lambda_memory_mb
  timeout       = var.lambda_timeout_s

  vpc_config {
    subnet_ids         = local.subnet_ids
    security_group_ids = [aws_security_group.batch_trigger_lambda.id]
  }

  dead_letter_config {
    target_arn = aws_sqs_queue.batch_trigger_dlq.arn
  }

  environment {
    variables = {
      TEMPORAL_ADDRESS              = local.temporal_address
      TEMPORAL_NAMESPACE            = var.temporal_namespace
      INCOMING_PREFIX               = var.incoming_prefix
      REPORT_ROOT                   = local.batch_report_root
      FLEET_REGION                  = var.region
      CPU_QUEUE_ASG_NAME            = aws_autoscaling_group.cpu_queue.name
      GPU_QUEUE_ASG_NAME            = aws_autoscaling_group.gpu_queue.name
      CPU_QUEUE_DESIRED             = tostring(var.cpu_queue_max_size)
      GPU_QUEUE_DESIRED             = tostring(var.gpu_queue_max_size)
      SHARD_SIZE                    = tostring(var.shard_size)
      SHARDS_IN_FLIGHT              = tostring(var.shards_in_flight)
      PDFS_PER_SHARD_IN_FLIGHT      = tostring(var.pdfs_per_shard_in_flight)
      WORKER_REGISTRATION_TIMEOUT_S = tostring(var.worker_registration_timeout_s)
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.batch_trigger_lambda_vpc,
    aws_iam_role_policy_attachment.batch_trigger_lambda,
    aws_cloudwatch_log_group.batch_trigger_lambda,
  ]
}
