# Hand the queue URL off to cpu-pipeline-01 via SSM. The ocr-ingestion
# systemd unit's ExecStartPre fetches this parameter at startup; until live/
# is applied (or after it's destroyed) the parameter is absent and the unit
# stays in restart-loop with a clear error, which is the desired signal.

resource "aws_ssm_parameter" "queue_url" {
  name        = "${local.live_ssm_prefix}/queue_url"
  description = "URL of the live PDF ingestion SQS queue. Consumed by ocr-ingestion on cpu-pipeline-01."
  type        = "String"
  value       = aws_sqs_queue.pdf_ingestion.url
}
