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

locals {
  artifact_bucket        = data.terraform_remote_state.shared_temporal.outputs.artifact_bucket
  cpu_pipeline_role_name = data.terraform_remote_state.shared_temporal.outputs.cpu_pipeline_role_name
  live_ssm_prefix        = data.terraform_remote_state.shared_temporal.outputs.live_ssm_prefix
  bucket_arn             = "arn:${data.aws_partition.current.partition}:s3:::${local.artifact_bucket}"
}
