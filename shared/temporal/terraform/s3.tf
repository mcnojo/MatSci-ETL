# Artifact bucket exists out-of-band (predates this module). We pull it via a
# data source so outputs surface it, but never modify it. Importing the bucket
# into terraform state is a future tightening — out of scope for Phase B.
data "aws_s3_bucket" "artifacts" {
  bucket = var.artifact_bucket
}
