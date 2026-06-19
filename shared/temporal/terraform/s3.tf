# Artifact bucket exists out-of-band (predates this module). We pull it via a
# data source so outputs surface it, but never modify it. Importing the bucket
# into terraform state is a future tightening — out of scope for Phase B.
data "aws_s3_bucket" "artifacts" {
  bucket = var.artifact_bucket
}

# Expire workflow-lifetime staging only. `assets/` holds page_elements dumps,
# rendered element PNGs, and the per-run pages.json/config.json the GPU lane
# reads from — none of it referenced after the owning workflow finalizes.
# Reports (batches/, live/reports/, comparisons/) and input PDFs (live/incoming/)
# live under other prefixes and are untouched.
resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = data.aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-assets-3d"
    status = "Enabled"

    filter {
      prefix = "assets/"
    }

    expiration {
      days = 3
    }

    # Multipart uploads from worker reboots mid-PUT — clean them up too.
    abort_incomplete_multipart_upload {
      days_after_initiation = 3
    }
  }
}
