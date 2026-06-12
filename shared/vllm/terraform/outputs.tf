output "instance_id" {
  description = "vLLM instance ID."
  value       = aws_instance.vllm.id
}

output "private_ip" {
  description = "Private IP. In-VPC workers (with OCR_VLLM_PREFER_PRIVATE_IP=1) route here."
  value       = aws_instance.vllm.private_ip
}

output "public_ip" {
  description = "Public IP. Mac-side callers (hybrid local-dev) route here."
  value       = aws_instance.vllm.public_ip
}

output "security_group_id" {
  description = "vLLM SG. Useful for cross-module rules."
  value       = aws_security_group.vllm.id
}

output "env_tag" {
  description = "dev | prod. Mirrors the input; lets downstream modules assert which box they're talking to."
  value       = var.env_tag
}

output "role_tag" {
  description = "Tag the resolver filters on (role=vllm-<model>-<env_tag>)."
  value       = local.role_tag
}

output "vllm_port" {
  description = "Port the vision vLLM unit is serving on."
  value       = var.vllm_port
}

output "tree_llm_port" {
  description = "Port the tree_llm vLLM unit is serving on."
  value       = var.tree_llm_port
}

output "tree_llm_role_tag" {
  description = "Secondary role tag the resolver filters on for tree_llm (tree_llm_role=vllm-<tree_llm_model_key>-<env_tag>)."
  value       = local.tree_llm_role_tag
}
