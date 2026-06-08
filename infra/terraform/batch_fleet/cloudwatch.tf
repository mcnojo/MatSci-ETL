resource "aws_cloudwatch_log_group" "batch_worker" {
  name              = "/${var.name_prefix}/worker"
  retention_in_days = 14
}
