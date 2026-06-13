output "region" {
  description = "Region the fleet was provisioned in."
  value       = data.aws_region.current.name
}

# Artifact bucket — re-exported from shared/temporal so the CLI can derive
# the manifest URI from a batch_id alone (single terraform_dir to read).
output "artifact_bucket" {
  description = "S3 bucket holding batch manifests + per-PDF outputs. Re-exported from shared/temporal so prod/batch/cli.py has a single terraform dir to read."
  value       = local.artifact_bucket
}

output "batch_report_root" {
  description = "S3 root for batch reports — s3://<artifact_bucket>."
  value       = local.batch_report_root
}

output "cpu_queue_asg_name" {
  description = "batch-cpu-tq ASG name. Source of truth for prod/batch/cli.py's fleet wiring (read via `terraform output`)."
  value       = aws_autoscaling_group.cpu_queue.name
}

output "gpu_queue_asg_name" {
  description = "batch-gpu-tq ASG name. Source of truth for prod/batch/cli.py's fleet wiring (read via `terraform output`)."
  value       = aws_autoscaling_group.gpu_queue.name
}

output "cpu_queue_asg_arn" {
  description = "batch-cpu-tq ASG ARN."
  value       = aws_autoscaling_group.cpu_queue.arn
}

output "gpu_queue_asg_arn" {
  description = "batch-gpu-tq ASG ARN."
  value       = aws_autoscaling_group.gpu_queue.arn
}

# Fleet bounds. The CLI sources the scale-up target from these via
# `terraform output` (prod/batch/cli.py). Reading off the ASG resource itself
# so the value matches what AWS will actually accept on SetDesiredCapacity.
output "cpu_queue_max_size" {
  description = "batch-cpu-tq ASG max_size. scale_fleet_up_activity passes this as cpu_queue_desired."
  value       = aws_autoscaling_group.cpu_queue.max_size
}

output "gpu_queue_max_size" {
  description = "batch-gpu-tq ASG max_size. scale_fleet_up_activity passes this as gpu_queue_desired."
  value       = aws_autoscaling_group.gpu_queue.max_size
}

output "worker_registration_timeout_s" {
  description = "Per-batch worker registration timeout. CLI plumbs this into BatchRunInput.worker_registration_timeout_s."
  value       = var.worker_registration_timeout_s
}

output "worker_role_arn" {
  description = "Instance-profile role ARN. Attach additional policies (e.g. KMS) here if needed."
  value       = aws_iam_role.batch_worker.arn
}

output "worker_security_group_id" {
  description = "Worker SG. Reference from other SGs to grant worker access."
  value       = aws_security_group.batch_worker.id
}

output "lifecycle_queue_url" {
  description = "Lifecycle hook SQS URL for a worker-side drain handler."
  value       = aws_sqs_queue.lifecycle_events.url
}

output "lifecycle_queue_arn" {
  description = "Lifecycle hook SQS ARN."
  value       = aws_sqs_queue.lifecycle_events.arn
}

output "log_group_name" {
  description = "CloudWatch log group for worker logs."
  value       = aws_cloudwatch_log_group.batch_worker.name
}
