# batch_trigger Lambda

S3 → `BatchRunWorkflow` fan-out. Fires on `s3:ObjectCreated` for
`<artifact_bucket>/batches/incoming/<batch_id>/manifest.json`, validates the
manifest, then starts a workflow keyed by `batch_id` with
`REJECT_DUPLICATE` (re-upload of the same manifest is a no-op).

## Layout

```
infra/lambdas/batch_trigger/
├── handler.py           # entry point (handler.handler)
├── requirements.txt     # temporalio, pydantic, pyyaml — boto3 is in the Lambda runtime
├── build.sh             # prepares build/bundle/ for terraform's archive_file
└── README.md
```

The handler imports only **leaf modules** that have no chain dependency on
Temporal activity / workflow classes (so the bundle stays small):

| Bundled module                        | Used for                                   |
| ------------------------------------- | ------------------------------------------ |
| `prod.batch.models`                   | `BatchManifest` pydantic validation        |
| `prod.batch.planner`                  | `batch_workflow_id` — deterministic ID     |
| `prod.batch.workflows.models`         | `BatchRunInput` — workflow input envelope  |
| `prod.shared_infra.task_queues`       | `CPU_TASK_QUEUE`, execution timeout const  |
| `shared.temporal_client`              | `connect_temporal` (pydantic data converter) |

The workflow itself is referenced **by string name** (`"BatchRunWorkflow"`)
when calling `client.start_workflow`, so the activity / workflow code never
ends up in the Lambda bundle.

## Configuration (env vars set by terraform)

| Var                              | Source / required                                                       |
| -------------------------------- | ----------------------------------------------------------------------- |
| `TEMPORAL_ADDRESS`               | required — cpu-pipeline-01 private IP + `:7233`                        |
| `TEMPORAL_NAMESPACE`             | optional, default `default`                                            |
| `INCOMING_PREFIX`                | required — e.g. `batches/incoming/`                                    |
| `REPORT_ROOT`                    | required — e.g. `s3://chem-lit-artifacts/batches`                      |
| `FLEET_REGION`                   | required                                                               |
| `CPU_QUEUE_ASG_NAME`             | required                                                               |
| `GPU_QUEUE_ASG_NAME`             | required                                                               |
| `CPU_QUEUE_DESIRED`              | required — workflow scales cpu ASG to this                             |
| `GPU_QUEUE_DESIRED`              | required — workflow scales gpu ASG to this                             |
| `SHARD_SIZE`                     | optional, default 50                                                   |
| `SHARDS_IN_FLIGHT`               | optional, default 8                                                    |
| `PDFS_PER_SHARD_IN_FLIGHT`       | optional, default 8                                                    |
| `WORKER_REGISTRATION_TIMEOUT_S`  | optional, default 600                                                  |

## Build → deploy

```bash
# 1. Build the bundle (copies source + pip-installs deps for the target arch).
infra/lambdas/batch_trigger/build.sh         # LAMBDA_ARCH=arm64 by default

# 2. Apply terraform — archive_file zips build/bundle/ at plan time.
bin/tf.sh batch init                          # one-time
bin/tf.sh batch plan
bin/tf.sh batch apply
```

The pipeline configs (`pipeline_config.yaml` + `prod_config.yaml`) are
bundled at step 1 — re-run `build.sh` whenever they change so the next
apply pushes a fresh Lambda.

## Failure modes

| Symptom                                       | Likely cause                                              |
| --------------------------------------------- | --------------------------------------------------------- |
| Lambda errors on import: `pydantic_core`      | bundle arch ≠ Lambda runtime arch (rebuild w/ correct `LAMBDA_ARCH`) |
| Lambda times out at Temporal connect          | cpu-pipeline-01 SG ingress not landed; check the cross-SG rule |
| Manifest puts go through but workflow never starts | check Lambda log group; common case is `batch_id` mismatch (manifest body vs S3 key path) |
| Persistent failures vanish                    | DLQ misconfigured — check `aws_sqs_queue.batch_trigger_dlq.url` |

## Re-trigger after fixing a failure

Re-upload the same `manifest.json` — the workflow ID is deterministic on
`batch_id`, and `REJECT_DUPLICATE` is caught and treated as success in the
handler. Idempotent.
