# Shared partial S3 backend config — every module's `terraform { backend "s3" {} }` block
# inherits these values via `terraform init -backend-config=../_backend.hcl`. Each module
# supplies its own `key` (state object name) so states never collide.
#
# The bucket + lock table are created out-of-band by `bin/bootstrap_tf_backend.sh`
# (chicken-and-egg: the backend can't be in terraform managing itself).
# Override the names there if `ocr-benchmarking-tfstate` is already taken globally.

bucket         = "ocr-benchmarking-tfstate"
region         = "us-west-2"
dynamodb_table = "ocr-benchmarking-tflock"
encrypt        = true
