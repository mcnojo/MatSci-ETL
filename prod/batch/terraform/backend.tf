terraform {
  backend "s3" {
    key = "batch/terraform.tfstate"
    # bucket, region, dynamodb_table, encrypt come from ../_backend.hcl
    # via `terraform init -backend-config=...` (handled by bin/tf.sh).
  }
}
