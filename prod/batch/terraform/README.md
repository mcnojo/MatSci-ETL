# `prod/batch/terraform`

Two Spot ASGs (`ocr-batch-cpu-queue` + `ocr-batch-gpu-queue`) for the batch motif. Inert until applied — both default `desired_capacity=0`. Both run CPU instances; gpu-queue workers are HTTP clients to vLLM (provisioned by `shared/vllm/terraform/`), no local GPU.

## Substrate

Reads `shared/temporal`'s tfstate for the artifact bucket, cpu-pipeline-01's SG/role/private IP; reads `shared/vllm`'s tfstate for each vLLM model's SG + port. Apply those first. `shared/vllm` is `try()`-guarded so out-of-order destroy plans cleanly.

## Apply

```bash
bin/tf.sh batch init
bin/tf.sh batch plan
bin/tf.sh batch apply
```

## What gets created

- Two ASGs — Spot, `min=0`, `max=2`, mixed-instances across 3 families × all default-VPC AZs, `capacity-optimized`. `capacity_rebalance=true` so replacements overlap drains. **No scaling policy** — `BatchRunWorkflow.scale_fleet_up_activity` writes `desired_capacity` per batch; the workflow's finally block zeroes it.
- Two launch templates on the latest AL2023 x86 AMI, IMDSv2-only, encrypted 50GB gp3 (sized for the torch + CUDA wheel install peak). Systemd unit caps torch/OMP/MKL at `torch_num_threads` (default 2) so concurrent doclayout-yolo calls don't oversubscribe vCPUs.
- Worker SG — no inbound (Session Manager). Ingress rules on out-of-module SGs: cpu-pipeline-01:7233 (Temporal) and one per vLLM model's port.
- SQS lifecycle queue + termination hooks, default `CONTINUE` (unsubscribed queue is harmless).
- IAM: worker role (S3 RW on bucket, EC2 describe for vLLM tag lookup, SSM + KMS-via-SSM for tree_llm key fetch, SQS drain, `CompleteLifecycleAction`, scoped Logs, `PutMetricData`, CW `ListMetrics`/`GetMetricData` for `build_report_activity`); lifecycle publisher role (ASG → SQS); cross-module attachment granting cpu-pipeline-01's role `autoscaling:SetDesiredCapacity` + `DescribeAutoScalingGroups` for `scale_fleet_*_activity`.
- CloudWatch: log group only (14-day retention). No custom metrics, no target tracking.

Submission is explicit (`bin/batch/submit.sh` → `python -m prod.batch.cli submit <batch_id>`) — no S3 → Lambda trigger. Trade: operator visibility for fast teardown.

## `user_data.sh.tpl`

Rendered twice (once per launch template) by `templatefile()` at plan time, differing only on `worker_role`. Vars: `repo_url`, `repo_ref`, `temporal_address`, `temporal_namespace`, `worker_role` (`cpu`|`gpu`), `max_concurrent_cpu`, `max_concurrent_gpu`, `torch_num_threads`, `aws_region`, `artifact_bucket`, `tree_llm_ssm_prefix`, `lifecycle_queue`, `log_group_name`, `log_collection_enabled`.

## Operate

```bash
# Session Manager
INSTANCE_ID=$(aws ec2 describe-instances --region us-west-2 \
  --filters "Name=tag:Name,Values=ocr-batch-cpu-queue-worker" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
aws ssm start-session --target "$INSTANCE_ID"

# Destroy — cross-SG ingress rules revert cleanly
bin/tf.sh batch destroy
```

## Cost

Idle: log group (free under 5GB/mo), SQS (free tier). Active: ~$0.04–0.09/hr per Spot instance, both ASGs.

## Gotchas

- **Spot quota below `max_size`** — apply succeeds, first scale-out hits `MaxSpotInstanceCountExceeded`. Fix in Service Quotas.
- **Workers can't reach Temporal** — check (a) ingress rule landed on cpu-pipeline-01's SG, (b) shared/temporal's private IP output is current (re-apply if cpu-pipeline-01 was recreated), (c) `nc -vz <addr>` from a worker.
- **Workers can't reach vLLM** — shared/vllm not applied, or its `models` output stale.

## Out of scope

`cpu-pipeline-01` (`shared/temporal/terraform/`); vLLM endpoint (`shared/vllm/terraform/`); `chem-lit-artifacts` bucket policy (out of band).
