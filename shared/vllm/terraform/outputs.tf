# One row per instance (keyed by primary role). Consumers iterate this map
# instead of reading scalar per-role outputs. Adding a third box is a
# one-line edit to var.models — every consumer (wait_health, live/batch
# up.sh) picks it up automatically. `services` enumerates all roles the box
# serves (primary + co-hosted secondaries) so wait_health can hit every port
# independently.
output "models" {
  description = "Per-instance vLLM deployment metadata. Keys match var.models keys (e.g. \"chandra\", \"gemma\")."
  value = {
    for k, inst in aws_instance.vllm : k => {
      instance_id       = inst.id
      public_ip         = inst.public_ip
      private_ip        = inst.private_ip
      security_group_id = aws_security_group.vllm[k].id
      instance_type     = var.models[k].instance_type
      services = [
        for svc in local.services_per_instance[k] : {
          role_key    = svc.key
          role_tag    = "vllm_role_${svc.key}" # EC2 tag key set to "true" on this instance
          hf_model_id = svc.hf_model_id
          hf_revision = svc.hf_revision
          port        = svc.port
        }
      ]
    }
  }
}
