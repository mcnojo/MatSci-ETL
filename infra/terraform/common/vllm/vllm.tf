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

# Deep Learning Base GPU AMI (NVIDIA, Ubuntu 22.04). Lookup is region-aware.
data "aws_ami" "dlami" {
  count       = var.ami_id == null ? 1 : 0
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

locals {
  ami_id    = var.ami_id != null ? var.ami_id : data.aws_ami.dlami[0].id
  subnet_id = coalesce(var.subnet_id, sort(data.aws_subnets.selected.ids)[0])
  role_tag  = "vllm-${var.model_key}-${var.env_tag}"
  user_data_vars = {
    hf_model_id     = var.hf_model_id
    vllm_port       = var.vllm_port
    vllm_extra_args = var.vllm_extra_args
  }
}

resource "aws_security_group" "vllm" {
  name        = "${var.name_prefix}-vllm-${var.model_key}-${var.env_tag}-sg"
  description = "vLLM serving SG for ${local.role_tag}. Egress all; ingress on vllm_port from operator_cidrs + worker SGs."
  vpc_id      = data.aws_vpc.selected.id

  egress {
    description      = "all egress"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  ingress {
    description = "vLLM port from operator CIDR(s)"
    from_port   = var.vllm_port
    to_port     = var.vllm_port
    protocol    = "tcp"
    cidr_blocks = var.operator_cidrs
  }

  dynamic "ingress" {
    for_each = var.allow_ssh_from_operator ? [1] : []
    content {
      description = "SSH from operator CIDR(s)"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = var.operator_cidrs
    }
  }
}

# Per-worker-SG ingress on the vLLM port. Scoped at this scope so destroying
# common/vllm cleanly removes the rules from the worker SGs.
resource "aws_security_group_rule" "vllm_from_workers" {
  for_each                 = toset(var.worker_security_group_ids)
  description              = "vLLM port from worker SG ${each.value}"
  type                     = "ingress"
  from_port                = var.vllm_port
  to_port                  = var.vllm_port
  protocol                 = "tcp"
  source_security_group_id = each.value
  security_group_id        = aws_security_group.vllm.id
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

resource "aws_iam_role" "vllm" {
  name               = "${var.name_prefix}-vllm-${var.model_key}-${var.env_tag}"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_instance_profile" "vllm" {
  name = "${var.name_prefix}-vllm-${var.model_key}-${var.env_tag}"
  role = aws_iam_role.vllm.name
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.vllm.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "vllm" {
  statement {
    sid       = "CloudWatchAgentMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "vllm" {
  name   = "${var.name_prefix}-vllm-${var.model_key}-${var.env_tag}"
  policy = data.aws_iam_policy_document.vllm.json
}

resource "aws_iam_role_policy_attachment" "vllm" {
  role       = aws_iam_role.vllm.name
  policy_arn = aws_iam_policy.vllm.arn
}

resource "aws_instance" "vllm" {
  ami                    = local.ami_id
  instance_type          = var.instance_type
  subnet_id              = local.subnet_id
  vpc_security_group_ids = [aws_security_group.vllm.id]
  iam_instance_profile   = aws_iam_instance_profile.vllm.name
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

  user_data = templatefile("${path.module}/user_data.sh.tpl", local.user_data_vars)

  # role tag is the resolver's primary key — keep it stable.
  tags = {
    Name  = "${var.name_prefix}-vllm-${var.model_key}-${var.env_tag}"
    role  = local.role_tag
    Model = var.model_key
    Env   = var.env_tag
  }

  lifecycle {
    ignore_changes = [user_data, ami]
  }
}
