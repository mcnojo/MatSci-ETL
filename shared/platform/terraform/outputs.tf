output "tree_llm_ssm_prefix" {
  description = "Prefix under which tree_llm key slots live. Downstream modules read params by name."
  value       = var.tree_llm_ssm_prefix
}

output "anthropic_api_key_param_name" {
  description = "Full SSM name for the Anthropic key slot."
  value       = aws_ssm_parameter.anthropic_api_key.name
}

output "openai_api_key_param_name" {
  description = "Full SSM name for the OpenAI key slot."
  value       = aws_ssm_parameter.openai_api_key.name
}

output "vllm_weights_bucket" {
  description = "Pre-staged vLLM weights bucket. Read by shared/vllm user_data (s3 sync) + IAM."
  value       = aws_s3_bucket.vllm_weights.bucket
}

output "vllm_weights_bucket_arn" {
  description = "Weights bucket ARN — for shared/vllm's read-scoped IAM policy."
  value       = aws_s3_bucket.vllm_weights.arn
}

output "stager_instance_profile_name" {
  description = "Instance profile bin/stage_model.sh attaches to its ephemeral EC2 stager."
  value       = aws_iam_instance_profile.stager.name
}
