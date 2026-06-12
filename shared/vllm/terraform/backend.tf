terraform {
  backend "s3" {
    key = "shared/vllm/terraform.tfstate"
    # bucket, region, dynamodb_table, encrypt come from ../../terraform/_backend.hcl
    # via `terraform init -backend-config=...` (handled by bin/tf.sh).
  }
}
