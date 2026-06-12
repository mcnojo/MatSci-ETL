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

# --- Phase D: batch_trigger Lambda outputs ---------------------------------

output "batch_trigger_lambda_arn" {
  description = "Batch trigger Lambda ARN. Reference from monitoring / alerting."
  value       = aws_lambda_function.batch_trigger.arn
}

output "batch_trigger_lambda_name" {
  description = "Batch trigger Lambda function name."
  value       = aws_lambda_function.batch_trigger.function_name
}

output "batch_trigger_security_group_id" {
  description = "Lambda SG. The cross-SG ingress rule on cpu-pipeline-01:7233 is owned by this module."
  value       = aws_security_group.batch_trigger_lambda.id
}

output "batch_trigger_dlq_url" {
  description = "DLQ for failed Lambda invocations (14-day retention)."
  value       = aws_sqs_queue.batch_trigger_dlq.url
}

output "batch_trigger_dlq_arn" {
  description = "DLQ ARN."
  value       = aws_sqs_queue.batch_trigger_dlq.arn
}

output "batch_trigger_log_group_name" {
  description = "CloudWatch log group for the batch trigger Lambda."
  value       = aws_cloudwatch_log_group.batch_trigger_lambda.name
}

output "lambda_bundle_dir" {
  description = "Path the operator must populate via prod/batch/lambdas/batch_trigger/build.sh before plan/apply."
  value       = local.lambda_bundle_dir
}
