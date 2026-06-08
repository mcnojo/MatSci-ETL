terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = merge(
      { Project = "ocr-benchmarking", Env = "batch" },
      var.tags,
    )
  }
}

data "aws_region" "current" {}
data "aws_partition" "current" {}

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

# One AMI covers both ASGs (both x86).
data "aws_ami" "al2023_x86" {
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
  subnet_ids = length(var.subnet_ids) > 0 ? var.subnet_ids : data.aws_subnets.selected.ids

  cpu_queue_asg_name = "${var.name_prefix}-cpu-queue"
  gpu_queue_asg_name = "${var.name_prefix}-gpu-queue"

  user_data_path = "${path.module}/../../../prod/batch/scripts/user_data.sh.tpl"
  user_data_common = {
    repo_url            = var.repo_url
    repo_ref            = var.repo_ref
    temporal_address    = var.temporal_address
    temporal_namespace  = var.temporal_namespace
    max_concurrent_cpu  = var.max_concurrent_cpu
    max_concurrent_gpu  = var.max_concurrent_gpu
    torch_num_threads   = var.torch_num_threads
    aws_region          = var.region
    artifact_bucket     = var.artifact_bucket
    tree_llm_ssm_prefix = var.tree_llm_ssm_prefix
    lifecycle_queue     = aws_sqs_queue.lifecycle_events.url
    log_group_name      = aws_cloudwatch_log_group.batch_worker.name
  }
  cpu_queue_user_data_vars = merge(local.user_data_common, { worker_role = "cpu" })
  gpu_queue_user_data_vars = merge(local.user_data_common, { worker_role = "gpu" })
}

resource "aws_security_group" "batch_worker" {
  name        = "${var.name_prefix}-worker-sg"
  description = "OCR batch worker. Egress all; no inbound (Session Manager handles operator access)."
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

# Scoped ingress on an out-of-module SG; destroy reverts cleanly.
resource "aws_security_group_rule" "temporal_from_workers" {
  description              = "Batch workers to Temporal on cpu-pipeline-01"
  type                     = "ingress"
  from_port                = 7233
  to_port                  = 7233
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.batch_worker.id
  security_group_id        = var.cpu_pipeline_security_group_id
}

resource "aws_sqs_queue" "lifecycle_events" {
  name                       = "${var.name_prefix}-lifecycle-events"
  message_retention_seconds  = 3600
  visibility_timeout_seconds = var.lifecycle_hook_heartbeat_s
}

resource "aws_launch_template" "cpu_queue" {
  name          = "${var.name_prefix}-cpu-queue-lt"
  image_id      = data.aws_ami.al2023_x86.id
  instance_type = var.cpu_queue_instance_types[0] # overridden per-launch by mixed-instances
  key_name      = var.key_pair_name

  iam_instance_profile {
    arn = aws_iam_instance_profile.batch_worker.arn
  }

  vpc_security_group_ids = [aws_security_group.batch_worker.id]

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  monitoring { enabled = true }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 30
      volume_type           = "gp3"
      delete_on_termination = true
      encrypted             = true
    }
  }

  user_data = base64encode(templatefile(local.user_data_path, local.cpu_queue_user_data_vars))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "${var.name_prefix}-cpu-queue-worker"
      Role = "batch-cpu-queue-worker"
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags          = { Name = "${var.name_prefix}-cpu-queue-worker-vol" }
  }
}

# gpu-task-queue worker: HTTP client to vLLM, no local GPU.
resource "aws_launch_template" "gpu_queue" {
  name          = "${var.name_prefix}-gpu-queue-lt"
  image_id      = data.aws_ami.al2023_x86.id
  instance_type = var.gpu_queue_instance_types[0]
  key_name      = var.key_pair_name

  iam_instance_profile {
    arn = aws_iam_instance_profile.batch_worker.arn
  }

  vpc_security_group_ids = [aws_security_group.batch_worker.id]

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  monitoring { enabled = true }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 30
      volume_type           = "gp3"
      delete_on_termination = true
      encrypted             = true
    }
  }

  user_data = base64encode(templatefile(local.user_data_path, local.gpu_queue_user_data_vars))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "${var.name_prefix}-gpu-queue-worker"
      Role = "batch-gpu-queue-worker"
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags          = { Name = "${var.name_prefix}-gpu-queue-worker-vol" }
  }
}

resource "aws_autoscaling_group" "cpu_queue" {
  name                = local.cpu_queue_asg_name
  min_size            = 0
  max_size            = var.cpu_queue_max_size
  desired_capacity    = 0
  vpc_zone_identifier = local.subnet_ids

  # Launch replacement at Spot interruption notice so capacity overlaps the drain.
  capacity_rebalance        = true
  health_check_type         = "EC2"
  health_check_grace_period = 300
  default_cooldown          = 60

  mixed_instances_policy {
    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.cpu_queue.id
        version            = "$Latest"
      }
      dynamic "override" {
        for_each = var.cpu_queue_instance_types
        content {
          instance_type = override.value
        }
      }
    }
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0
      spot_allocation_strategy                 = var.spot_allocation_strategy
    }
  }

  enabled_metrics = [
    "GroupDesiredCapacity",
    "GroupInServiceInstances",
    "GroupPendingInstances",
    "GroupTerminatingInstances",
    "GroupTotalInstances",
  ]

  tag {
    key                 = "Name"
    value               = local.cpu_queue_asg_name
    propagate_at_launch = false
  }

  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 50
      instance_warmup        = 300
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_autoscaling_group" "gpu_queue" {
  name                = local.gpu_queue_asg_name
  min_size            = 0
  max_size            = var.gpu_queue_max_size
  desired_capacity    = 0
  vpc_zone_identifier = local.subnet_ids

  capacity_rebalance        = true
  health_check_type         = "EC2"
  health_check_grace_period = 300
  default_cooldown          = 60

  mixed_instances_policy {
    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.gpu_queue.id
        version            = "$Latest"
      }
      dynamic "override" {
        for_each = var.gpu_queue_instance_types
        content {
          instance_type = override.value
        }
      }
    }
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0
      spot_allocation_strategy                 = var.spot_allocation_strategy
    }
  }

  enabled_metrics = [
    "GroupDesiredCapacity",
    "GroupInServiceInstances",
    "GroupPendingInstances",
    "GroupTerminatingInstances",
    "GroupTotalInstances",
  ]

  tag {
    key                 = "Name"
    value               = local.gpu_queue_asg_name
    propagate_at_launch = false
  }

  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 50
      instance_warmup        = 300
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

# CONTINUE default: an unsubscribed lifecycle queue is harmless.
resource "aws_autoscaling_lifecycle_hook" "cpu_queue_terminating" {
  name                    = "${var.name_prefix}-cpu-queue-terminating"
  autoscaling_group_name  = aws_autoscaling_group.cpu_queue.name
  lifecycle_transition    = "autoscaling:EC2_INSTANCE_TERMINATING"
  default_result          = "CONTINUE"
  heartbeat_timeout       = var.lifecycle_hook_heartbeat_s
  notification_target_arn = aws_sqs_queue.lifecycle_events.arn
  role_arn                = aws_iam_role.lifecycle_publisher.arn
}

resource "aws_autoscaling_lifecycle_hook" "gpu_queue_terminating" {
  name                    = "${var.name_prefix}-gpu-queue-terminating"
  autoscaling_group_name  = aws_autoscaling_group.gpu_queue.name
  lifecycle_transition    = "autoscaling:EC2_INSTANCE_TERMINATING"
  default_result          = "CONTINUE"
  heartbeat_timeout       = var.lifecycle_hook_heartbeat_s
  notification_target_arn = aws_sqs_queue.lifecycle_events.arn
  role_arn                = aws_iam_role.lifecycle_publisher.arn
}
