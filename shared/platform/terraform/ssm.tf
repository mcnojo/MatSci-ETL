# Long-lived SecureString slots for tree_llm API keys. Created with a
# placeholder; operator populates the real value once via:
#
#   aws ssm put-parameter --name /ocr-bench/tree_llm/anthropic_api_key \
#       --type SecureString --value "$KEY" --overwrite
#
# These resources intentionally live in `shared/platform` — a module that
# `bin/<motif>/down.sh` never touches — so the operator-populated value
# survives nightly compute teardown. `ignore_changes = [value]` keeps
# subsequent platform applies from clobbering it back to the placeholder.
# `prevent_destroy = true` is a belt-and-suspenders guard against a stray
# `terraform destroy` in this module.

resource "aws_ssm_parameter" "anthropic_api_key" {
  name        = "${var.tree_llm_ssm_prefix}/anthropic_api_key"
  description = "Anthropic API key for tree_llm. Populate via `aws ssm put-parameter --overwrite`."
  type        = "SecureString"
  value       = "PLACEHOLDER_OPERATOR_OVERWRITES"

  lifecycle {
    ignore_changes  = [value]
    prevent_destroy = true
  }
}

resource "aws_ssm_parameter" "openai_api_key" {
  name        = "${var.tree_llm_ssm_prefix}/openai_api_key"
  description = "OpenAI API key for tree_llm. Populate via `aws ssm put-parameter --overwrite`."
  type        = "SecureString"
  value       = "PLACEHOLDER_OPERATOR_OVERWRITES"

  lifecycle {
    ignore_changes  = [value]
    prevent_destroy = true
  }
}
