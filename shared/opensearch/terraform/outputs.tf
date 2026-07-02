output "endpoint" {
  description = "HTTPS URL. Feed into pipeline_config.yaml:retrieval.opensearch.endpoint."
  value       = "https://${aws_instance.os.private_ip}:9200"
}

output "public_endpoint" {
  description = "HTTPS URL over the box's public IP. Only useful when operator_cidrs opens 9200; workers should always use the private endpoint."
  value       = aws_instance.os.public_ip != "" ? "https://${aws_instance.os.public_ip}:9200" : null
}

output "instance_id" {
  value = aws_instance.os.id
}

output "security_group_id" {
  description = "Attach to worker SGs to grant OS ingress when the workers stand up in a separate module."
  value       = aws_security_group.os.id
}

output "admin_password_param" {
  description = "SSM SecureString name. Workers export the value into OPENSEARCH_PASSWORD; the admin username is 'admin'."
  value       = aws_ssm_parameter.admin_password.name
}

output "snapshot_bucket" {
  value = local.snapshot_bkt
}
