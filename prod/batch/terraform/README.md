# `prod/batch/terraform`

Two Spot ASGs (cpu-task-queue + gpu-task-queue) for the batch motif. Inert
until applied — both ASGs default to `desired_capacity=0`.

Both ASGs run CPU instances. The gpu-task-queue workers are HTTP clients to
vLLM — no local GPU. The vLLM endpoint lives on its own box, provisioned by
`shared/vllm/terraform/`.

## Substrate dependency

Reads `shared/temporal/`'s tfstate via `terraform_remote_state` for the
artifact bucket, cpu-pipeline-01 SG, and private IP. Apply `shared/temporal`
first; nothing to wire by hand.

## Apply

```bash
bin/tf.sh batch init
bin/tf.sh batch plan
bin/tf.sh batch apply
```

## What gets created (~20 resources)

- `ocr-batch-cpu-queue` + `ocr-batch-gpu-queue` ASGs — Spot, `min=0`, `max=2`, `capacity-optimized` across 3 instance families × all default-VPC AZs. `capacity_rebalance=true` so replacements overlap drains. **No scaling policy** — `cli run` writes `desired_capacity` per batch and zeroes it at end (`cli teardown-fleet` for force-stop).
- Two launch templates on the latest AL2023 x86 AMI, IMDSv2-only, encrypted 30GB gp3. Systemd unit caps torch/OMP/MKL threads at `torch_num_threads` (default 2) so concurrent doclayout-yolo calls don't oversubscribe vCPUs.
- Worker SG — no inbound (Session Manager); one ingress rule on `cpu_pipeline_security_group_id:7233` from the worker SG.
- SQS lifecycle queue + termination hooks. Default action `CONTINUE` — unsubscribed queue is harmless.
- IAM: worker (S3 on bucket, EC2 describe for vLLM tag lookup, SQS drain, SSM, scoped Logs); lifecycle publisher (ASG → SQS).
- CloudWatch: log group only. No custom metrics, no target tracking.

## Dependencies (must land before apply)

**`./user_data.sh.tpl`** (colocated with this module). `templatefile()` reads at plan time. Rendered twice — once per launch template, differing only on `worker_role`. Variables:

| Var | Type |
|---|---|
| `repo_url`, `repo_ref` | string |
| `temporal_address`, `temporal_namespace` | string |
| `worker_role` | string — `"cpu"` or `"gpu"`; selects which queue this instance polls |
| `max_concurrent_cpu`, `max_concurrent_gpu`, `torch_num_threads` | number |
| `aws_region`, `artifact_bucket`, `lifecycle_queue`, `log_group_name` | string |

## Operate

```bash
# Shell in via Session Manager (no SSH, no open ports)
INSTANCE_ID=$(aws ec2 describe-instances --region us-west-2 \
  --filters "Name=tag:Name,Values=ocr-batch-cpu-queue-worker" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
aws ssm start-session --target "$INSTANCE_ID"

# Destroy — cleanly removes the cross-SG ingress rule
bin/tf.sh batch destroy
```

## Cost

Idle: CloudWatch log group (free under 5GB/mo); SQS under free tier.
Active: ~$0.04–0.09/hr per Spot instance (c7i.large–xlarge), both ASGs.

## Gotchas

- **Spot quota below `max_size`** — apply succeeds, first scale-out fails (`MaxSpotInstanceCountExceeded`). Fix via Service Quotas, no terraform change.
- **Workers can't reach Temporal** — check (a) ingress rule landed on the right SG, (b) the private IP from shared/temporal's remote_state is current (re-apply shared/temporal if cpu-pipeline-01 was recreated), (c) Session Manager in + `nc -vz <addr>`.
- **`templatefile` error at plan** — Step 2 hasn't landed.

## Out of scope

`cpu-pipeline-01` (managed by `shared/temporal/terraform/`); vLLM endpoint (managed by `shared/vllm/terraform/`); bucket policy on `chem-lit-artifacts` (out of band).
