# `common/temporal`

Production substrate shared by both motifs: `cpu-pipeline-01` (Temporal + live
consumer + report builder), SSM parameters for `tree_llm` API keys, the
artifact S3 bucket.

**Skeleton only — populated in Phase B of `AWS_DEPLOYMENT_PLAN.md`.** The
backend is wired so `bin/tf.sh common/temporal init` succeeds today; once
`cpu_pipeline.tf`, `ssm.tf`, `s3.tf`, `outputs.tf` land, `apply` brings up the
substrate. Outputs (cpu-pipeline-01 private IP, SG ID, bucket name) get
consumed by `live/` and `batch/` via `terraform_remote_state`.

Split from `common/vllm` so hybrid local-dev (Mac drives, AWS hosts only vLLM)
doesn't need this module applied at all.
