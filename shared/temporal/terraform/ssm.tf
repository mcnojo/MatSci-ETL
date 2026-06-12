# tree_llm API key slots are owned by `shared/platform` (see
# shared/platform/terraform/README.md). This module only references them by
# name — the cpu_pipeline IAM policy already grants ssm:GetParameter on
# `${tree_llm_ssm_prefix}/*`, so the on-box bootstrap fetches values at boot.
#
# Data sources here exist purely to (a) fail at plan time if the slots are
# missing (caller forgot to apply shared/platform first), and (b) surface
# the names as outputs for any downstream module that wants them.
data "aws_ssm_parameter" "anthropic_api_key" {
  name = "${var.tree_llm_ssm_prefix}/anthropic_api_key"
}

data "aws_ssm_parameter" "openai_api_key" {
  name = "${var.tree_llm_ssm_prefix}/openai_api_key"
}
