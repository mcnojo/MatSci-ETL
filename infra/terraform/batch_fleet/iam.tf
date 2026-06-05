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
    sid     = "ArtifactBucketObjects"
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = [
      "arn:${data.aws_partition.current.partition}:s3:::${var.artifact_bucket}/*",
    ]
  }

  statement {
    sid     = "ArtifactBucketList"
    actions = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [
      "arn:${data.aws_partition.current.partition}:s3:::${var.artifact_bucket}",
    ]
  }

  # Tag/instance describe is not resource-scopable.
  statement {
    sid       = "DescribeForVllmResolution"
    actions   = ["ec2:DescribeInstances", "ec2:DescribeTags"]
    resources = ["*"]
  }

  # Pre-staged for a future worker-side drain handler; unused today.
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
}

resource "aws_iam_policy" "batch_worker" {
  name   = "${var.name_prefix}-worker"
  policy = data.aws_iam_policy_document.batch_worker.json
}

resource "aws_iam_role_policy_attachment" "batch_worker" {
  role       = aws_iam_role.batch_worker.name
  policy_arn = aws_iam_policy.batch_worker.arn
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
