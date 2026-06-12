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

# AZ-scoped subnet pool: only fetched when var.availability_zone is set. The
# postcondition turns "no subnet in that AZ" into a clear plan-time error
# instead of a silent fallback that ignores the operator's intent.
data "aws_subnets" "in_az" {
  count = var.availability_zone != null ? 1 : 0
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.selected.id]
  }
  filter {
    name   = "availability-zone"
    values = [var.availability_zone]
  }
  lifecycle {
    postcondition {
      condition     = length(self.ids) > 0
      error_message = "availability_zone=\"${var.availability_zone}\" matched no subnets in the selected VPC."
    }
  }
}

# Singular subnet lookup to read the AZ for the offerings preflight.
data "aws_subnet" "chosen" {
  id = local.subnet_id
}

# Plan-time guard: does this AZ actually offer var.instance_type? Catches
# "g6e isn't sold in us-west-2c" before RunInstances. Does NOT catch "sold
# but currently saturated" — the 3-min create timeout below handles that.
data "aws_ec2_instance_type_offerings" "in_az" {
  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
  filter {
    name   = "location"
    values = [data.aws_subnet.chosen.availability_zone]
  }
  location_type = "availability-zone"
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
  ami_id = var.ami_id != null ? var.ami_id : data.aws_ami.dlami[0].id
  # 3-tier subnet pick: explicit subnet_id > AZ-name shortcut > lex-first fallback.
  subnet_id = (
    var.subnet_id != null ? var.subnet_id :
    var.availability_zone != null ? data.aws_subnets.in_az[0].ids[0] :
    sort(data.aws_subnets.selected.ids)[0]
  )
  role_tag          = "vllm-${var.model_key}-${var.env_tag}"
  tree_llm_role_tag = "vllm-${var.tree_llm_model_key}-${var.env_tag}"
  user_data_vars = {
    hf_model_id                     = var.hf_model_id
    vllm_port                       = var.vllm_port
    vllm_extra_args                 = var.vllm_extra_args
    vision_max_model_len            = var.vision_max_model_len
    vision_gpu_memory_utilization   = var.vision_gpu_memory_utilization
    tree_llm_hf_model_id            = var.tree_llm_hf_model_id
    tree_llm_port                   = var.tree_llm_port
    tree_llm_extra_args             = var.tree_llm_extra_args
    tree_llm_max_model_len          = var.tree_llm_max_model_len
    tree_llm_gpu_memory_utilization = var.tree_llm_gpu_memory_utilization
    aws_region                      = var.region
    gpu_metrics_interval            = var.gpu_metrics_interval_s
    gpu_metrics_namespace           = var.gpu_metrics_namespace
  }
}

resource "aws_security_group" "vllm" {
  name        = "${var.name_prefix}-vllm-${var.model_key}-${var.env_tag}-sg"
  description = "vLLM serving SG for ${local.role_tag}. Egress all; operator + worker ingress attached as standalone rules below."
  vpc_id      = data.aws_vpc.selected.id

  egress {
    description      = "all egress"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  # No inline ingress — matches shared/temporal. Standalone rules below are
  # gated on `length(operator_cidrs) > 0` so the fail-closed default is real:
  # empty operator_cidrs → no operator rules → SSM Session Manager is the only
  # access path. External modules attaching their own ingress to this SG won't
  # be clobbered by re-applies.
}

resource "aws_security_group_rule" "vision_from_operator" {
  count             = length(var.operator_cidrs) > 0 ? 1 : 0
  description       = "vision vLLM port from operator CIDR(s)"
  type              = "ingress"
  from_port         = var.vllm_port
  to_port           = var.vllm_port
  protocol          = "tcp"
  cidr_blocks       = var.operator_cidrs
  security_group_id = aws_security_group.vllm.id
}

resource "aws_security_group_rule" "tree_llm_from_operator" {
  count             = length(var.operator_cidrs) > 0 ? 1 : 0
  description       = "tree_llm vLLM port from operator CIDR(s)"
  type              = "ingress"
  from_port         = var.tree_llm_port
  to_port           = var.tree_llm_port
  protocol          = "tcp"
  cidr_blocks       = var.operator_cidrs
  security_group_id = aws_security_group.vllm.id
}

resource "aws_security_group_rule" "ssh_from_operator" {
  count             = (var.allow_ssh_from_operator && length(var.operator_cidrs) > 0) ? 1 : 0
  description       = "SSH from operator CIDR(s)"
  type              = "ingress"
  from_port         = 22
  to_port           = 22
  protocol          = "tcp"
  cidr_blocks       = var.operator_cidrs
  security_group_id = aws_security_group.vllm.id
}

# Per-worker-SG ingress on each vLLM port. Scoped at this scope so destroying
# shared/vllm cleanly removes the rules from the worker SGs.
resource "aws_security_group_rule" "vllm_from_workers" {
  for_each                 = toset(var.worker_security_group_ids)
  description              = "vision vLLM port from worker SG ${each.value}"
  type                     = "ingress"
  from_port                = var.vllm_port
  to_port                  = var.vllm_port
  protocol                 = "tcp"
  source_security_group_id = each.value
  security_group_id        = aws_security_group.vllm.id
}

resource "aws_security_group_rule" "tree_llm_from_workers" {
  for_each                 = toset(var.worker_security_group_ids)
  description              = "tree_llm vLLM port from worker SG ${each.value}"
  type                     = "ingress"
  from_port                = var.tree_llm_port
  to_port                  = var.tree_llm_port
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

  # Two role tags: shared/vllm/resolve.py walks both, so callers can write
  # vllm-instance://chandra:8004/v1 (vision) AND vllm-instance://gemma:8005/v1
  # (tree_llm) against the same box.
  tags = {
    Name          = "${var.name_prefix}-vllm-${var.model_key}-${var.env_tag}"
    role          = local.role_tag
    tree_llm_role = local.tree_llm_role_tag
    Model         = var.model_key
    TreeLlmModel  = var.tree_llm_model_key
    Env           = var.env_tag
  }

  # Cap the silent RunInstances retry loop. Default is 10m; capacity errors
  # surface in ≤3m here. Bounds only the EC2 state→running wait — user_data
  # (model downloads, vLLM warmup) runs post-running and isn't affected.
  timeouts {
    create = "3m"
  }

  lifecycle {
    ignore_changes = [user_data, ami]
    precondition {
      condition     = length(data.aws_ec2_instance_type_offerings.in_az.instance_types) > 0
      error_message = "instance_type ${var.instance_type} is not offered in AZ ${data.aws_subnet.chosen.availability_zone}. Pick a different AZ via --zone (e.g. us-west-2a/2b/2d) or change var.instance_type."
    }
  }
}
