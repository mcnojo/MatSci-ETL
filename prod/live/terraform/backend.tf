terraform {
  backend "s3" {
    key = "live/terraform.tfstate"
    # bucket, region, dynamodb_table, encrypt come from
    # shared/terraform/_backend.hcl via `terraform init -backend-config=...`
    # (handled by bin/tf.sh).
  }
}
