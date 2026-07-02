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

# Amazon Linux 2023 ARM64 — matches the default instance_type (t4g.medium).
data "aws_ssm_parameter" "al2023" {
  count = var.ami_id == null ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-arm64"
}

locals {
  ami_id       = var.ami_id != null ? var.ami_id : data.aws_ssm_parameter.al2023[0].value
  subnet_id    = var.subnet_id != null ? var.subnet_id : sort(data.aws_subnets.selected.ids)[0]
  role_tag     = "opensearch"
  snapshot_bkt = var.snapshot_bucket_name != null ? var.snapshot_bucket_name : "${var.name_prefix}-opensearch-snapshots"
}

# Admin password — random unless the operator passes one via SSM SecureString
# outside the module. The value written to SSM is what user_data feeds into
# OPENSEARCH_INITIAL_ADMIN_PASSWORD; workers read the same param.
resource "random_password" "admin" {
  length      = 24
  special     = true
  min_special = 2
  # OpenSearch 2.12+ enforces a min password length / complexity for admin.
  override_special = "!@#%^*()_-+="
}

resource "aws_ssm_parameter" "admin_password" {
  name        = "/${var.name_prefix}/opensearch/admin_password"
  description = "OpenSearch admin password. Consumed by workers via retrieval.opensearch.password_env indirection."
  type        = "SecureString"
  value       = random_password.admin.result
}

# Security group

resource "aws_security_group" "os" {
  name        = "${var.name_prefix}-opensearch-sg"
  description = "OpenSearch single-node SG. Operator ingress via operator_cidrs; worker ingress via SG-to-SG rules."
  vpc_id      = data.aws_vpc.selected.id

  egress {
    description = "all egress"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group_rule" "os_from_operator" {
  count             = length(var.operator_cidrs) > 0 ? 1 : 0
  description       = "OpenSearch 9200 from operator CIDR(s)"
  type              = "ingress"
  from_port         = 9200
  to_port           = 9200
  protocol          = "tcp"
  cidr_blocks       = var.operator_cidrs
  security_group_id = aws_security_group.os.id
}

resource "aws_security_group_rule" "os_from_workers" {
  count                    = length(var.worker_security_group_ids)
  description              = "OpenSearch 9200 from worker SG"
  type                     = "ingress"
  from_port                = 9200
  to_port                  = 9200
  protocol                 = "tcp"
  source_security_group_id = var.worker_security_group_ids[count.index]
  security_group_id        = aws_security_group.os.id
}

# IAM: SSM Session Manager + snapshot bucket access

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "os" {
  name               = "${var.name_prefix}-opensearch"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_instance_profile" "os" {
  name = "${var.name_prefix}-opensearch"
  role = aws_iam_role.os.name
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.os.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "snapshot" {
  # OpenSearch's S3 repo plugin reads/writes objects under the bucket root.
  statement {
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:${data.aws_partition.current.partition}:s3:::${local.snapshot_bkt}"]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
    resources = ["arn:${data.aws_partition.current.partition}:s3:::${local.snapshot_bkt}/*"]
  }
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.admin_password.arn]
  }
}

resource "aws_iam_policy" "os" {
  name   = "${var.name_prefix}-opensearch"
  policy = data.aws_iam_policy_document.snapshot.json
}

resource "aws_iam_role_policy_attachment" "os" {
  role       = aws_iam_role.os.name
  policy_arn = aws_iam_policy.os.arn
}

# Snapshot bucket

resource "aws_s3_bucket" "snapshots" {
  count  = var.create_snapshot_bucket ? 1 : 0
  bucket = local.snapshot_bkt
}

resource "aws_s3_bucket_versioning" "snapshots" {
  count  = var.create_snapshot_bucket ? 1 : 0
  bucket = aws_s3_bucket.snapshots[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "snapshots" {
  count  = var.create_snapshot_bucket ? 1 : 0
  bucket = aws_s3_bucket.snapshots[0].id
  rule {
    id     = "expire-noncurrent"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 14
    }
  }
}

# Instance

resource "aws_instance" "os" {
  ami                    = local.ami_id
  instance_type          = var.instance_type
  subnet_id              = local.subnet_id
  vpc_security_group_ids = [aws_security_group.os.id]
  iam_instance_profile   = aws_iam_instance_profile.os.name
  key_name               = var.key_pair_name

  # Spot when requested. hibernate=false: OpenSearch is stateless-ish across
  # boot (index lives on EBS); snapshots to S3 buy back durability across
  # interruptions.
  dynamic "instance_market_options" {
    for_each = var.use_spot ? [1] : []
    content {
      market_type = "spot"
      spot_options {
        max_price                      = var.spot_max_price
        instance_interruption_behavior = "terminate"
      }
    }
  }

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

  user_data = templatefile("${path.module}/user_data.sh.tpl", {
    opensearch_version   = var.opensearch_version
    admin_password_param = aws_ssm_parameter.admin_password.name
    snapshot_bucket      = local.snapshot_bkt
    aws_region           = var.region
  })

  tags = {
    Name = "${var.name_prefix}-opensearch"
    role = local.role_tag
  }

  lifecycle {
    ignore_changes = [user_data, ami]
  }
}
