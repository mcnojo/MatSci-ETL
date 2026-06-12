# `shared/temporal/terraform`

Shared substrate that both motifs depend on. Provisions cpu-pipeline-01 — an
EC2 m7i box whose user_data installs Temporal server, Postgres, the always-on
cpu-task-queue worker, and the live consumer + reports timer as systemd
units. Also owns the tree_llm SSM SecureString slots and the artifact-bucket
data source.

| Resource                            | Purpose                                                            |
| ----------------------------------- | ------------------------------------------------------------------ |
| `aws_instance.cpu_pipeline`         | The m7i CPU box.                                                   |
| `aws_security_group.cpu_pipeline`   | Egress all; ingress from `var.operator_cidrs` only.                |
| `aws_iam_role.cpu_pipeline` + attach| Instance role: S3, SSM, CW Agent, EC2 describe, KMS.               |
| `aws_eip.cpu_pipeline`              | Stable IP for the Temporal UI.                                     |
| `aws_cloudwatch_log_group`          | Worker + Temporal logs.                                            |
| `aws_ssm_parameter.{anthropic,openai}_api_key` | Placeholder SecureStrings — operator overwrites.        |

The artifact S3 bucket is pre-existing (`var.artifact_bucket`); referenced via
a data source so its name/ARN surface in outputs but it's never modified.

## Why this lives in `shared/`

Both motifs depend on cpu-pipeline-01:
- live runs the consumer + the ProcessPdfWorkflow worker on the box
- batch's BatchRunWorkflow + ShardWorkflow parents run on the box's Temporal
  server; the batch ASGs only handle the per-PDF activity fan-out

So neither motif "owns" the box. Keeping it in `shared/temporal/terraform/`
lets either motif's `up.sh` apply it before applying motif-specific resources,
and lets either motif's `down.sh` skip destroying the box if the other motif
might still want it.
