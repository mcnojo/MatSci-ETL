output "cpu_pipeline_instance_id" {
  description = "cpu-pipeline-01 instance ID. Pass to `aws ssm start-session --target` for shell access."
  value       = aws_instance.cpu_pipeline.id
}

output "cpu_pipeline_private_ip" {
  description = "cpu-pipeline-01 private IP. Use for worker temporal_address (workers in the same VPC)."
  value       = aws_instance.cpu_pipeline.private_ip
}

output "cpu_pipeline_public_ip" {
  description = "cpu-pipeline-01 Elastic IP. Operator-facing for Temporal UI."
  value       = aws_eip.cpu_pipeline.public_ip
}

output "cpu_pipeline_security_group_id" {
  description = "cpu-pipeline-01 SG. Other modules add ingress rules (e.g. batch workers on :7233)."
  value       = aws_security_group.cpu_pipeline.id
}

output "cpu_pipeline_role_arn" {
  description = "cpu-pipeline-01 instance-profile role ARN."
  value       = aws_iam_role.cpu_pipeline.arn
}

output "vpc_id" {
  description = "VPC the substrate lives in. Consumed by live/ and batch/ modules."
  value       = data.aws_vpc.selected.id
}

output "subnet_id" {
  description = "Subnet cpu-pipeline-01 is in."
  value       = local.subnet_id
}

output "artifact_bucket" {
  description = "Existing S3 artifact bucket name."
  value       = data.aws_s3_bucket.artifacts.bucket
}

output "tree_llm_ssm_prefix" {
  description = "SSM prefix under which tree_llm API keys live. Workers fetch on boot."
  value       = var.tree_llm_ssm_prefix
}

output "log_group_name" {
  description = "CloudWatch log group for cpu-pipeline-01."
  value       = aws_cloudwatch_log_group.cpu_pipeline.name
}
