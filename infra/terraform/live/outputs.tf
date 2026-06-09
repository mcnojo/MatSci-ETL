output "queue_url" {
  description = "URL of the PDF ingestion SQS queue."
  value       = aws_sqs_queue.pdf_ingestion.url
}

output "queue_arn" {
  description = "ARN of the PDF ingestion SQS queue."
  value       = aws_sqs_queue.pdf_ingestion.arn
}

output "incoming_prefix" {
  description = "S3 prefix that fires the SQS notification."
  value       = var.incoming_prefix
}

output "ssm_queue_url_parameter" {
  description = "SSM parameter name that holds the queue URL. ocr-ingestion fetches this at boot."
  value       = aws_ssm_parameter.queue_url.name
}
