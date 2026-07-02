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

# AZ-scoped subnet pool — only fetched when var.availability_zone is set. The
# postcondition turns "no subnet in that AZ" into a clear plan-time error
# instead of silently falling back and ignoring the operator's intent.
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

# Singular subnet lookup to read the AZ for the per-model offerings preflight.
data "aws_subnet" "chosen" {
  id = local.subnet_id
}

# Plan-time guard: does this AZ actually offer each model's instance type?
# Catches "g6e isn't sold in us-west-2c" before RunInstances. Does NOT catch
# "sold but currently saturated" — the 3-min create timeout on the instance
# handles that.
data "aws_ec2_instance_type_offerings" "in_az" {
  for_each = var.models
  filter {
    name   = "instance-type"
    values = [each.value.instance_type]
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
  # 3-tier subnet pick: explicit subnet_id > AZ-name shortcut > lex-first
  # fallback. Shared across every model in this module.
  subnet_id = (
    var.subnet_id != null ? var.subnet_id :
    var.availability_zone != null ? data.aws_subnets.in_az[0].ids[0] :
    sort(data.aws_subnets.selected.ids)[0]
  )
  role_tags = {
    for k, _ in var.models : k => "vllm-${k}"
  }
}

# One SG per model. Strict least-privilege: each SG only opens that model's
# port to operator_cidrs, so an inbound rule for chandra can't accidentally
# reach gemma. Consumer modules (prod/batch, prod/live) attach worker-SG
# ingress via the per-model security_group_ids output.
resource "aws_security_group" "vllm" {
  for_each    = var.models
  name        = "${var.name_prefix}-vllm-${each.key}-sg"
  description = "vLLM serving SG for ${local.role_tags[each.key]}. Egress all; operator ingress as standalone rules; consumer modules attach their own worker-SG ingress via the security_group_ids output."
  vpc_id      = data.aws_vpc.selected.id

  egress {
    description      = "all egress"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
  # No inline ingress: operator rules below are gated on operator_cidrs so
  # the fail-closed default is real (empty -> SSM-only). Worker-SG ingress is
  # owned by consumer modules (prod/batch, prod/live) to avoid apply-order
  # dependencies (workers don't exist yet when vllm applies).
}

resource "aws_security_group_rule" "vllm_from_operator" {
  for_each          = length(var.operator_cidrs) > 0 ? var.models : {}
  description       = "vLLM port for ${each.key} from operator CIDR(s)"
  type              = "ingress"
  from_port         = each.value.port
  to_port           = each.value.port
  protocol          = "tcp"
  cidr_blocks       = var.operator_cidrs
  security_group_id = aws_security_group.vllm[each.key].id
}

resource "aws_security_group_rule" "ssh_from_operator" {
  for_each          = (var.allow_ssh_from_operator && length(var.operator_cidrs) > 0) ? var.models : {}
  description       = "SSH from operator CIDR(s) — ${each.key} box"
  type              = "ingress"
  from_port         = 22
  to_port           = 22
  protocol          = "tcp"
  cidr_blocks       = var.operator_cidrs
  security_group_id = aws_security_group.vllm[each.key].id
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

# One IAM role per model. Could be shared (the policy is identical), but a
# per-instance role keeps the principal-of-least-name straight in CloudTrail
# and lets a per-model policy fork in later without restructuring.
resource "aws_iam_role" "vllm" {
  for_each           = var.models
  name               = "${var.name_prefix}-vllm-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_instance_profile" "vllm" {
  for_each = var.models
  name     = "${var.name_prefix}-vllm-${each.key}"
  role     = aws_iam_role.vllm[each.key].name
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  for_each   = var.models
  role       = aws_iam_role.vllm[each.key].name
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
  for_each = var.models
  name     = "${var.name_prefix}-vllm-${each.key}"
  policy   = data.aws_iam_policy_document.vllm.json
}

resource "aws_iam_role_policy_attachment" "vllm" {
  for_each   = var.models
  role       = aws_iam_role.vllm[each.key].name
  policy_arn = aws_iam_policy.vllm[each.key].arn
}

resource "aws_instance" "vllm" {
  for_each = var.models

  ami                    = local.ami_id
  instance_type          = each.value.instance_type
  subnet_id              = local.subnet_id
  vpc_security_group_ids = [aws_security_group.vllm[each.key].id]
  iam_instance_profile   = aws_iam_instance_profile.vllm[each.key].name
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

  user_data = templatefile("${path.module}/user_data.sh.tpl", {
    hf_model_id            = each.value.hf_model_id
    vllm_port              = each.value.port
    vllm_extra_args        = each.value.extra_args
    max_model_len          = each.value.max_model_len
    gpu_memory_utilization = each.value.gpu_memory_utilization
    aws_region             = var.region
    gpu_metrics_interval   = var.gpu_metrics_interval_s
    gpu_metrics_namespace  = var.gpu_metrics_namespace
  })

  # Single role tag — shared/vllm/resolve.py filters on `role=vllm-<key>`
  # and finds exactly one match per model.
  tags = {
    Name  = "${var.name_prefix}-vllm-${each.key}"
    role  = local.role_tags[each.key]
    Model = each.key
  }

  # Cap the silent RunInstances retry loop. Default is 10m; capacity errors
  # surface in ≤3m here. Bounds only the EC2 state->running wait — user_data
  # (model download, vLLM warmup) runs post-running and isn't affected.
  timeouts {
    create = "3m"
  }

  lifecycle {
    ignore_changes = [user_data, ami]
    precondition {
      condition     = length(data.aws_ec2_instance_type_offerings.in_az[each.key].instance_types) > 0
      error_message = "models[\"${each.key}\"].instance_type ${each.value.instance_type} is not offered in AZ ${data.aws_subnet.chosen.availability_zone}. Pick a different AZ via --zone (e.g. us-west-2a/2b/2d) or change that model's instance_type."
    }
  }
}
