# prod/ — AWS deployment bundle

Pipeline logic lives in `etl/pipeline/`.  This directory wraps it in Temporal workflows, SQS ingestion, and deployment scripts.

## Architecture

```
S3 raw-pdfs -> SQS -> ingestion consumer -> Temporal ProcessPdfWorkflow
                                              ├─ CPU activities (cpu-task-queue)
                                              └─ GPU activities (gpu-task-queue)
                                          -> S3 artifacts
```

Two EC2 hosts:


| Host            | Type       | Role                                               |
| --------------- | ---------- | -------------------------------------------------- |
| cpu-pipeline-01 | m7i.xlarge | Temporal + Postgres (Docker), worker, SQS consumer |
| gpu-model-01    | g6.2xlarge | vLLM model server (chandra + text `LLM)            |


## Local development

```bash
# 1. Start Temporal + Postgres
make infra          # docker compose up -d

# 2. Start worker (handles both task queues)
make worker         # python -m prod.worker

# 3. Run a workflow
python -m etl.cli --pdf etl/hybrid.pdf

# 4. Temporal UI at http://localhost:8233
```

## Prod setup (one-time)

```bash
# On cpu-pipeline-01:
./prod/scripts/setup_cpu.sh
```

This installs Docker, starts Temporal + Postgres via docker-compose and creates a systemd unit for the worker.

## SQS ingestion

```bash
# On cpu-pipeline-01:
python -m prod.ingestion.consumer
```

## Scripts

```bash
./prod/scripts/spin_up.sh              # start instances, wait for Temporal health
./prod/scripts/spin_down.sh            # stop instances (preserves EBS)
./prod/scripts/teardown.sh             # terminate instances + delete EBS volumes
./prod/scripts/lockdown_sg.sh          # tighten GPU security group
```

## Spin-down vs teardown

**Spin-down** (`spin_down.sh`) — stop instances, preserve EBS volumes (~$10/mo for 100GB gp3). Use this for daily start/stop cycles.

**Teardown** (`teardown.sh`) — terminate instances, destroy EBS volumes. Use this when you're done for an extended period. Reprovision from scratch with `launch.sh` + `setup_cpu.sh`.

Postgres data is Temporal-only (transient) — nothing is lost on teardown.