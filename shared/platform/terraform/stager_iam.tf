# Ephemeral stager EC2 role — bin/stage_model.sh runs a one-shot instance
# under this profile that pulls HF weights and syncs them to the weights
# bucket, then self-terminates. Scoped to models/* + _stager_logs/* on the
# bucket + read of the HF token param.

data "aws_iam_policy_document" "stager_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "stager" {
  name               = "ocr-bench-stager"
  assume_role_policy = data.aws_iam_policy_document.stager_assume.json
}

resource "aws_iam_instance_profile" "stager" {
  name = "ocr-bench-stager"
  role = aws_iam_role.stager.name
}

# SSM core: operator can `aws ssm start-session` in for live log tail.
resource "aws_iam_role_policy_attachment" "stager_ssm_core" {
  role       = aws_iam_role.stager.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "stager" {
  statement {
    sid = "WeightsWrite"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
    ]
    resources = [
      "${aws_s3_bucket.vllm_weights.arn}/models/*",
      "${aws_s3_bucket.vllm_weights.arn}/_stager_logs/*",
    ]
  }
  statement {
    sid       = "WeightsList"
    actions   = ["s3:ListBucket", "s3:ListBucketMultipartUploads"]
    resources = [aws_s3_bucket.vllm_weights.arn]
  }
  statement {
    sid       = "HFToken"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:*:*:parameter/ocr-bench/hf_token"]
  }
}

resource "aws_iam_policy" "stager" {
  name   = "ocr-bench-stager"
  policy = data.aws_iam_policy_document.stager.json
}

resource "aws_iam_role_policy_attachment" "stager" {
  role       = aws_iam_role.stager.name
  policy_arn = aws_iam_policy.stager.arn
}
