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
