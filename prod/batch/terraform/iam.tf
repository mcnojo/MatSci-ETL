data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "batch_worker" {
  name               = "${var.name_prefix}-worker"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_instance_profile" "batch_worker" {
  name = "${var.name_prefix}-worker"
  role = aws_iam_role.batch_worker.name
}

# Session Manager: keyless operator access.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.batch_worker.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "batch_worker" {
  statement {
    sid       = "ArtifactBucketObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${local.bucket_arn}/*"]
  }

  statement {
    sid       = "ArtifactBucketList"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [local.bucket_arn]
  }

  # Tag/instance describe is not resource-scopable.
  statement {
    sid       = "DescribeForVllmResolution"
    actions   = ["ec2:DescribeInstances", "ec2:DescribeTags"]
    resources = ["*"]
  }

  # tree_llm key fetch at boot.
  statement {
    sid     = "TreeLlmSsmRead"
    actions = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = [
      "arn:${data.aws_partition.current.partition}:ssm:${var.region}:*:parameter${var.tree_llm_ssm_prefix}/*",
    ]
  }

  statement {
    sid       = "SsmKmsDecrypt"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }

  # Drain handler reads from the lifecycle SQS queue.
  statement {
    sid = "LifecycleQueueDrain"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.lifecycle_events.arn]
  }

  statement {
    sid       = "CompleteLifecycleAction"
    actions   = ["autoscaling:CompleteLifecycleAction"]
    resources = ["*"] # not resource-scopable
  }

  statement {
    sid = "WorkerLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.batch_worker.arn}:*"]
  }

  # CWAgent publishes into OCR/Batch/Worker; PutMetricData is not resource-scopable.
  statement {
    sid       = "CloudWatchAgentMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
  }

  # build_report_activity walks CloudWatch when it lands on a batch worker.
  # Neither call is resource-scopable.
  statement {
    sid       = "CloudWatchReportReads"
    actions   = ["cloudwatch:ListMetrics", "cloudwatch:GetMetricData"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "batch_worker" {
  name   = "${var.name_prefix}-worker"
  policy = data.aws_iam_policy_document.batch_worker.json
}

resource "aws_iam_role_policy_attachment" "batch_worker" {
  role       = aws_iam_role.batch_worker.name
  policy_arn = aws_iam_policy.batch_worker.arn
}

# Cross-module IAM grant: scale_fleet_up/down activities run on cpu-pipeline-01
# (whose role is owned by shared/temporal) and call SetDesiredCapacity on the
# batch ASGs (owned by this module). The policy is created here and attached
# to the shared/temporal role so tearing batch down detaches the grant cleanly
# without touching the role itself — same precedent as prod/live/terraform/iam.tf.
data "aws_iam_policy_document" "batch_scaling" {
  statement {
    sid     = "SetDesiredCapacityOnBatchAsgs"
    actions = ["autoscaling:SetDesiredCapacity"]
    resources = [
      aws_autoscaling_group.cpu_queue.arn,
      aws_autoscaling_group.gpu_queue.arn,
    ]
  }
  # DescribeAutoScalingGroups does not support resource-level scoping in IAM
  # (AWS API limitation), so it's "*". scale_fleet_down_activity polls this
  # to confirm the ASG instance list is empty before returning — closes the
  # rescue race where a fast resubmit re-targets about-to-die boxes.
  statement {
    sid       = "DescribeAsgsForDrainWait"
    actions   = ["autoscaling:DescribeAutoScalingGroups"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "batch_scaling" {
  name        = "${var.name_prefix}-scaling-from-cpu-pipeline"
  description = "Grants cpu-pipeline-01 SetDesiredCapacity on the two batch ASGs (scale_fleet activities)."
  policy      = data.aws_iam_policy_document.batch_scaling.json
}

resource "aws_iam_role_policy_attachment" "batch_scaling" {
  role       = local.cpu_pipeline_role_name
  policy_arn = aws_iam_policy.batch_scaling.arn
}

# Role assumed by ASG to publish lifecycle events to SQS.
data "aws_iam_policy_document" "asg_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["autoscaling.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lifecycle_publisher" {
  name               = "${var.name_prefix}-lifecycle-publisher"
  assume_role_policy = data.aws_iam_policy_document.asg_assume.json
}

data "aws_iam_policy_document" "lifecycle_publisher" {
  statement {
    actions   = ["sqs:SendMessage", "sqs:GetQueueUrl"]
    resources = [aws_sqs_queue.lifecycle_events.arn]
  }
}

resource "aws_iam_role_policy" "lifecycle_publisher" {
  name   = "${var.name_prefix}-lifecycle-publisher"
  role   = aws_iam_role.lifecycle_publisher.id
  policy = data.aws_iam_policy_document.lifecycle_publisher.json
}
