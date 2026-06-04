# prod/ — AWS deployment

Two operating modes share the same Temporal workflows and activity registry:

| Subpackage          | Mode                | Optimizes for                           |
| ------------------- | ------------------- | --------------------------------------- |
| `prod/live/`        | streaming service   | per-PDF latency (SQS-driven, always-on) |
| `prod/batch/`       | bounded bulk jobs   | GPU utilization across a full corpus    |
| `prod/shared_infra/`| common building blocks | task queues, retry policies, activity I/O models |

Pipeline logic itself lives in `etl/pipeline/`. The `prod/` tree wraps it in
deployment-specific orchestration.

See `prod/live/README.md` and `prod/batch/README.md` for mode-specific
operator docs.
