data "aws_vpc" "selected" {
  id      = var.vpc_id
  default = var.vpc_id == null ? true : null
}

data "aws_subnets" "selected" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.selected.id]
  }
}

# AL2023 x86_64. Matches batch worker AMI family — same docker/python toolchain.
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-kernel-6.1-x86_64"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

locals {
  subnet_id      = coalesce(var.subnet_id, sort(data.aws_subnets.selected.ids)[0])
  user_data_path = "${path.module}/user_data.sh.tpl"
  user_data_vars = {
    repo_url               = var.repo_url
    repo_ref               = var.repo_ref
    aws_region             = var.region
    artifact_bucket        = var.artifact_bucket
    tree_llm_ssm_prefix    = var.tree_llm_ssm_prefix
    live_ssm_prefix        = var.live_ssm_prefix
    log_group_name         = aws_cloudwatch_log_group.cpu_pipeline.name
    log_collection_enabled = var.log_collection_enabled ? "1" : "0"
    max_concurrent_cpu     = var.max_concurrent_cpu
    max_concurrent_gpu     = var.max_concurrent_gpu
    torch_num_threads      = var.torch_num_threads
  }
}

# Operator access on SSH/UI/gRPC. Empty list = no inbound; Session Manager
# is the supported access path.
resource "aws_security_group" "cpu_pipeline" {
  name        = "${var.name_prefix}-cpu-pipeline-sg"
  description = "cpu-pipeline-01: Temporal + Postgres + live consumer + worker. Egress all; ingress from operator_cidrs only; workers add their own ingress rule via SG attachment."
  vpc_id      = data.aws_vpc.selected.id

  egress {
    description      = "all egress"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  # No inline ingress blocks — all ingress is managed via standalone
  # aws_security_group_rule resources below so that external modules
  # (prod/batch) can attach their own rules without Terraform clobbering them.
}

# Operator ingress: conditional on operator_cidrs being non-empty.
# Standalone rules so prod/batch can safely add its own rules to this SG.
resource "aws_security_group_rule" "ssh_from_operator" {
  count             = length(var.operator_cidrs) > 0 ? 1 : 0
  description       = "SSH from operator"
  type              = "ingress"
  from_port         = 22
  to_port           = 22
  protocol          = "tcp"
  cidr_blocks       = var.operator_cidrs
  security_group_id = aws_security_group.cpu_pipeline.id
}

resource "aws_security_group_rule" "temporal_grpc_from_operator" {
  count             = length(var.operator_cidrs) > 0 ? 1 : 0
  description       = "Temporal gRPC from operator"
  type              = "ingress"
  from_port         = 7233
  to_port           = 7233
  protocol          = "tcp"
  cidr_blocks       = var.operator_cidrs
  security_group_id = aws_security_group.cpu_pipeline.id
}

resource "aws_security_group_rule" "temporal_ui_from_operator" {
  count             = length(var.operator_cidrs) > 0 ? 1 : 0
  description       = "Temporal UI from operator"
  type              = "ingress"
  from_port         = 8233
  to_port           = 8233
  protocol          = "tcp"
  cidr_blocks       = var.operator_cidrs
  security_group_id = aws_security_group.cpu_pipeline.id
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cpu_pipeline" {
  name               = "${var.name_prefix}-cpu-pipeline"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_instance_profile" "cpu_pipeline" {
  name = "${var.name_prefix}-cpu-pipeline"
  role = aws_iam_role.cpu_pipeline.name
}

# Session Manager — operator-side keyless access.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.cpu_pipeline.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "cpu_pipeline" {
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

  # tree_llm key fetch at boot + on rotation.
  statement {
    sid     = "TreeLlmSsmRead"
    actions = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = [
      "arn:${data.aws_partition.current.partition}:ssm:${var.region}:*:parameter${var.tree_llm_ssm_prefix}/*",
    ]
  }

  # SecureString decrypt uses the AWS-managed SSM KMS key by default — grant
  # the alias so GetParameter with WithDecryption=true succeeds.
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

  # vLLM resolver tag lookup.
  statement {
    sid       = "DescribeForVllmResolution"
    actions   = ["ec2:DescribeInstances", "ec2:DescribeTags"]
    resources = ["*"]
  }

  statement {
    sid = "CpuPipelineLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.cpu_pipeline.arn}:*"]
  }

  # CWAgent publishes to OCR/Live/Worker; PutMetricData is not resource-scopable.
  statement {
    sid       = "CloudWatchAgentMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
  }

  # The scheduled ocr-live-report.timer (and ad-hoc `python -m prod.reports`
  # runs on the box) walk CloudWatch for worker + GPU stats. ListMetrics is
  # required to enumerate per-instance dimensions; GetMetricData reads them.
  # Neither is resource-scopable.
  statement {
    sid       = "CloudWatchReportReads"
    actions   = ["cloudwatch:ListMetrics", "cloudwatch:GetMetricData"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "cpu_pipeline" {
  name   = "${var.name_prefix}-cpu-pipeline"
  policy = data.aws_iam_policy_document.cpu_pipeline.json
}

resource "aws_iam_role_policy_attachment" "cpu_pipeline" {
  role       = aws_iam_role.cpu_pipeline.name
  policy_arn = aws_iam_policy.cpu_pipeline.arn
}

resource "aws_cloudwatch_log_group" "cpu_pipeline" {
  name              = "/${var.name_prefix}/cpu-pipeline"
  retention_in_days = 14
}

resource "aws_instance" "cpu_pipeline" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  subnet_id              = local.subnet_id
  vpc_security_group_ids = [aws_security_group.cpu_pipeline.id]
  iam_instance_profile   = aws_iam_instance_profile.cpu_pipeline.name
  key_name               = var.key_pair_name

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  monitoring = true

  root_block_device {
    volume_size           = var.root_volume_gb
    volume_type           = "gp3"
    delete_on_termination = true
    encrypted             = true
  }

  user_data = templatefile(local.user_data_path, local.user_data_vars)

  tags = {
    Name = "${var.name_prefix}-cpu-pipeline-01"
    Role = "cpu-pipeline"
  }

  # user_data changes alone shouldn't replace a healthy box — re-apply by
  # tainting if a re-bootstrap is genuinely needed.
  lifecycle {
    ignore_changes = [user_data, ami]
  }
}

resource "aws_eip" "cpu_pipeline" {
  domain   = "vpc"
  instance = aws_instance.cpu_pipeline.id
  tags = {
    Name = "${var.name_prefix}-cpu-pipeline-01"
  }
}
