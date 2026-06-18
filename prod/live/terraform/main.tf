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
      { Project = "ocr-benchmarking", Env = "live" },
      var.tags,
    )
  }
}

data "aws_region" "current" {}
data "aws_partition" "current" {}

# shared/temporal owns the artifact bucket + cpu-pipeline-01 + its IAM role.
# We read both: the bucket name/ARN for the S3 → SQS notification permission,
# and the role name for cross-module SQS + SSM grants attached to the consumer
# that already runs on cpu-pipeline-01.
data "terraform_remote_state" "shared_temporal" {
  backend = "s3"
  config = {
    bucket         = var.state_bucket
    key            = "shared/temporal/terraform.tfstate"
    region         = var.state_region
    dynamodb_table = var.state_lock_table
    encrypt        = true
  }
}

# shared/vllm owns the prod vLLM box's SG. We attach cpu_pipeline → vLLM
# ingress here so the live worker on cpu-pipeline-01 can reach the vision
# and tree_llm units via the private IP path.
data "terraform_remote_state" "shared_vllm" {
  backend = "s3"
  config = {
    bucket         = var.state_bucket
    key            = "shared/vllm/terraform.tfstate"
    region         = var.state_region
    dynamodb_table = var.state_lock_table
    encrypt        = true
  }
}

locals {
  artifact_bucket                = data.terraform_remote_state.shared_temporal.outputs.artifact_bucket
  cpu_pipeline_role_name         = data.terraform_remote_state.shared_temporal.outputs.cpu_pipeline_role_name
  cpu_pipeline_security_group_id = data.terraform_remote_state.shared_temporal.outputs.cpu_pipeline_security_group_id
  live_ssm_prefix                = data.terraform_remote_state.shared_temporal.outputs.live_ssm_prefix
  bucket_arn                     = "arn:${data.aws_partition.current.partition}:s3:::${local.artifact_bucket}"

  # shared/vllm's models map — one entry per vLLM instance, each carrying its
  # SG id + port. try() so `terraform destroy` still plans when shared/vllm
  # was already destroyed (out-of-order down). Empty map → for_each below
  # creates no rules and skips cleanly.
  vllm_models = try(data.terraform_remote_state.shared_vllm.outputs.models, {})
}

# cpu-pipeline-01 → vLLM ingress, one rule per model. Worker + live consumer
# both call vLLM via private IP from this box; without these rules connect_tcp
# times out.
resource "aws_security_group_rule" "vllm_from_cpu_pipeline" {
  for_each                 = local.vllm_models
  description              = "cpu-pipeline-01 to vLLM ${each.key} port"
  type                     = "ingress"
  from_port                = each.value.port
  to_port                  = each.value.port
  protocol                 = "tcp"
  source_security_group_id = local.cpu_pipeline_security_group_id
  security_group_id        = each.value.security_group_id
}
