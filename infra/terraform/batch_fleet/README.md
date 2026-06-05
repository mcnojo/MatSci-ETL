# `batch_fleet`

Spot ASGs (CPU + GPU) for `prod/batch/`. Phase 5 of `INFRA_PROVISIONING_PLAN.md`. Inert until applied.

## Required vars

| Var | How to fetch |
|---|---|
| `artifact_bucket` | Live's bucket: `chem-lit-artifacts`. |
| `temporal_address` | `cpu-pipeline-01`'s **private** IP + `:7233`. Public exits the VPC. |
| `cpu_pipeline_security_group_id` | `cpu-pipeline-01`'s SG. One ingress rule on 7233 is added to it. |

```bash
aws ec2 describe-instances --region us-west-2 \
  --filters "Name=tag:Name,Values=ocr-bench-cpu-pipeline-01" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].[PrivateIpAddress,SecurityGroups[0].GroupId]' \
  --output text
```

## Apply

```bash
cd infra/terraform/batch_fleet
cat > terraform.tfvars <<EOF
artifact_bucket                = "chem-lit-artifacts"
temporal_address               = "10.0.x.y:7233"
cpu_pipeline_security_group_id = "sg-xxxxxxxxxxxxxxxxx"
EOF
terraform init
terraform plan
terraform apply
```

## What gets created (~25 resources)

- CPU + GPU ASGs — Spot, `min=0`, `max=2`, `capacity-optimized` across 3 instance families × all default-VPC AZs. `capacity_rebalance=true` so replacements overlap drains.
- Two launch templates on the latest AL2023 x86 AMI, IMDSv2-only, encrypted gp3 (30GB CPU / 100GB GPU), same IAM profile + SG.
- Worker SG — no inbound (Session Manager); one ingress rule on `cpu_pipeline_security_group_id:7233` from the worker SG.
- SQS lifecycle queue + termination hooks. Default action `CONTINUE` — unsubscribed queue is harmless.
- IAM: worker (S3 on bucket, EC2 describe for vLLM tag lookup, SQS drain, SSM, scoped Logs); lifecycle publisher (ASG → SQS).
- CloudWatch: log group + target-tracking on `OCR/Batch/QueueDepth / MAX(InService, 1)`, target = 4.

## Dependencies (must land before apply)

**`prod/batch/scripts/user_data.sh.tpl`** (Step 2). `templatefile()` reads at plan time. Variables passed in:

| Var | Type |
|---|---|
| `repo_url`, `repo_ref` | string |
| `temporal_address`, `temporal_namespace` | string |
| `max_concurrent_cpu`, `max_concurrent_gpu` | number |
| `aws_region`, `artifact_bucket`, `lifecycle_queue` | string |

**`queue_depth_publisher.py`** (Step 3) on `cpu-pipeline-01`. Without it, target tracking sees missing data and holds capacity. `submit_batch.sh` (Step 4) sidesteps by setting desired capacity directly.

## Operate

```bash
# Shell in via Session Manager (no SSH, no open ports)
INSTANCE_ID=$(aws ec2 describe-instances --region us-west-2 \
  --filters "Name=tag:Name,Values=ocr-batch-cpu-worker" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
aws ssm start-session --target "$INSTANCE_ID"

# Live queue depth
aws cloudwatch get-metric-statistics \
  --namespace OCR/Batch --metric-name QueueDepth \
  --dimensions Name=Queue,Value=gpu-task-queue \
  --start-time "$(date -u -v -10M +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time   "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 --statistics Average

# Destroy — cleanly removes the cross-SG ingress rule
terraform destroy
```

## Cost

Idle: ~$0.30/mo × 2 custom metrics; CloudWatch/SQS under free tier.
Active: ~$0.04/hr × CPU `max_size` Spot; ~$0.30/hr × GPU `max_size` Spot.

## Gotchas

- **Spot quota below `max_size`** — apply succeeds, first scale-out fails (`MaxSpotInstanceCountExceeded`). Fix via Service Quotas, no terraform change.
- **Workers can't reach Temporal** — check (a) ingress rule landed on the right SG, (b) `temporal_address` is private, (c) Session Manager in + `nc -vz <addr>`.
- **`templatefile` error at plan** — Step 2 hasn't landed.

## Out of scope

`cpu-pipeline-01` (managed by `prod/live/scripts/setup_cpu.sh`); vLLM endpoint (Phase 6); bucket policy on `chem-lit-artifacts` (out of band).
