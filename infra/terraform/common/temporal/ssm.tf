# SecureString slots for tree_llm API keys. Created with a placeholder value;
# operator populates the real key via:
#
#   aws ssm put-parameter --name /ocr-bench/tree_llm/anthropic_api_key \
#       --type SecureString --value "$KEY" --overwrite
#
# `ignore_changes = [value]` so subsequent terraform applies don't reset the
# operator-populated value back to the placeholder.

resource "aws_ssm_parameter" "anthropic_api_key" {
  name        = "${var.tree_llm_ssm_prefix}/anthropic_api_key"
  description = "Anthropic API key for tree_llm. Populate via `aws ssm put-parameter --overwrite`."
  type        = "SecureString"
  value       = "PLACEHOLDER_OPERATOR_OVERWRITES"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "openai_api_key" {
  name        = "${var.tree_llm_ssm_prefix}/openai_api_key"
  description = "OpenAI API key for tree_llm. Populate via `aws ssm put-parameter --overwrite`."
  type        = "SecureString"
  value       = "PLACEHOLDER_OPERATOR_OVERWRITES"

  lifecycle {
    ignore_changes = [value]
  }
}
