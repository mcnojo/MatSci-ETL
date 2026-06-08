output "region" {
  description = "Region the fleet was provisioned in."
  value       = data.aws_region.current.name
}

output "cpu_queue_asg_name" {
  description = "cpu-task-queue ASG name. Wire into batch_config.yaml fleet.cpu_queue_asg_name."
  value       = aws_autoscaling_group.cpu_queue.name
}

output "gpu_queue_asg_name" {
  description = "gpu-task-queue ASG name. Wire into batch_config.yaml fleet.gpu_queue_asg_name."
  value       = aws_autoscaling_group.gpu_queue.name
}

output "cpu_queue_asg_arn" {
  description = "cpu-task-queue ASG ARN."
  value       = aws_autoscaling_group.cpu_queue.arn
}

output "gpu_queue_asg_arn" {
  description = "gpu-task-queue ASG ARN."
  value       = aws_autoscaling_group.gpu_queue.arn
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
