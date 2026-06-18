# One row per model. Consumers iterate this map instead of reading scalar
# per-role outputs. Adding a third model is a one-line edit to var.models —
# every consumer (wait_health, dev/up_vllm) picks it up automatically.
output "models" {
  description = "Per-model deployment metadata. Keys match var.models keys (e.g. \"chandra\", \"gemma\")."
  value = {
    for k, inst in aws_instance.vllm : k => {
      instance_id       = inst.id
      public_ip         = inst.public_ip
      private_ip        = inst.private_ip
      port              = var.models[k].port
      role_tag          = local.role_tags[k]
      security_group_id = aws_security_group.vllm[k].id
      instance_type     = var.models[k].instance_type
      hf_model_id       = var.models[k].hf_model_id
    }
  }
}

output "env_tag" {
  description = "dev | prod. Mirrors the input; lets downstream modules assert which env they're talking to."
  value       = var.env_tag
}
