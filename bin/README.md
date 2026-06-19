# `bin/` — operator entry points

Two motifs, one shape:

```
bin/<motif>/up.sh        # bring it online
bin/<motif>/submit.sh …  # drop work into S3; the system does the rest
bin/<motif>/down.sh      # turn everything off
```

The operator never types `terraform`, `ssh`, or a Temporal CLI invocation for
the happy path.

| Motif    | When to use                                         | What submit does                          |
| -------- | --------------------------------------------------- | ----------------------------------------- |
| `batch`  | bounded jobs of 10–10,000+ PDFs, throughput-tuned  | upload PDFs + manifest -> Lambda fires    |
| `live`   | bursty arrivals, per-PDF latency-sensitive          | upload PDF -> S3->SQS->consumer fires       |
| `dev`    | hybrid local-dev (Mac drives, AWS hosts vLLM only)  | runs `etl/cli.py` from Mac as usual       |

---

## First-time setup (once per AWS account)

```bash
# 1. Create the S3 bucket + DynamoDB lock table for terraform state.
bin/bootstrap_tf_backend.sh

# 2. Apply shared/platform — long-lived SSM key slots that survive every
#    nightly down.sh. Owned by its own state, never touched by motif
#    up/down once set up.
bin/tf.sh shared/platform init
bin/tf.sh shared/platform apply

# 3. Populate the tree_llm API keys in the slots created above. Done once;
#    `lifecycle.ignore_changes = [value]` protects the values from being
#    reset by subsequent applies, and `prevent_destroy = true` protects
#    them from accidental destroys.
aws ssm put-parameter \
    --name /ocr-bench/tree_llm/anthropic_api_key \
    --type SecureString --overwrite --value "sk-ant-..."
aws ssm put-parameter \
    --name /ocr-bench/tree_llm/openai_api_key \
    --type SecureString --overwrite --value "sk-..."

# 4. The artifact bucket (chem-lit-artifacts) is read by terraform via a data
#    source — pre-create it manually if it doesn't already exist:
aws s3 mb s3://chem-lit-artifacts --region us-west-2
```

---

## Normal usage

### Batch

```bash
bin/batch/up.sh                                   # ~10 min cold (vLLM model dl)
bin/batch/submit.sh ./pdfs/ --batch-id my-corpus  # uploads then triggers Lambda
# … wait for the workflow to complete (watch in Temporal UI URL printed above) …
bin/batch/down.sh                                  # ~2 min
```

`bin/batch/submit.sh` validates the manifest with the same Pydantic model the
workflow uses, so duplicate or unsanitizable filenames are caught before any
upload. PDFs upload first, manifest LAST (the Lambda fires only on the
manifest PUT).

### Live

```bash
bin/live/up.sh
bin/live/submit.sh ./single.pdf ./batch-folder/   # mix of files + folders OK
bin/live/down.sh
```

Each PDF upload triggers a `ProcessPdfWorkflow` execution within a few seconds.

### Dev (hybrid local-dev)

```bash
bin/dev/up_vllm.sh
# vllm public IP echoed. Run etl/cli.py from your Mac as usual; it resolves
# the vision_server URL via EC2 tag lookup (role=vllm-chandra-dev).
bin/dev/down_vllm.sh
```

No Temporal, no SSM read, no batch fleet. Operator continues to run
`etl/cli.py` with Ollama locally for `tree_llm`.

---

## Recovery

| Symptom                                            | What to do                                                                     |
| -------------------------------------------------- | ------------------------------------------------------------------------------ |
| `up.sh` failed partway                             | re-run it. Terraform is idempotent; the failed step picks up where it left off |
| Lambda fires but workflow never appears in Temporal | check Lambda log group; common cause is REJECT_DUPLICATE on a re-uploaded id   |
| `submit.sh` says "document_id collision"           | rename the colliding PDFs (filenames sanitize to lowercase alphanumeric+hyphen) |
| live consumer keeps restarting                     | likely the SSM `queue_url` param doesn't exist — re-apply `bin/tf.sh live`     |
| Workflow stuck "waiting for workers"               | check ASG `Desired` vs. quota; `bin/tf.sh batch output` shows ASG names        |
| Want to nuke everything                             | `bin/<motif>/down.sh` is always safe — `shared/platform` (SSM key slots) and the artifact S3 bucket are never touched |

To fully clear an account (very rare):

```bash
# shared/platform has prevent_destroy on the SSM slots — drop that lifecycle
# block first, then:
bin/tf.sh shared/platform destroy

aws s3 rb s3://chem-lit-artifacts --force      # destructive
aws s3 rb s3://ocr-benchmarking-tfstate --force
aws dynamodb delete-table --table-name ocr-benchmarking-tflock
```

---

## Cost shape

| Resource                | Active                | Idle (down.sh run)        |
| ----------------------- | --------------------- | ------------------------- |
| cpu-pipeline-01 (m7i.xlarge) | ~$0.20/hr        | $0                        |
| vLLM box (g6.xlarge)         | ~$0.93/hr        | $0                        |
| Batch fleet ASGs (Spot)      | ~$0.06–0.20/hr each | $0 (scaled to 0)       |
| Lambda + SQS + DLQ           | negligible (<1¢/day) | <1¢/day                |
| S3 artifact bucket           | $0.023/GB-month  | same                      |
| Terraform state bucket       | <1¢/month        | same                      |

The dominant ongoing cost when the motif is up is the GPU box. `down.sh`
brings the bill to near zero (only S3 + DDB linger).

---

## File map

```
bin/
├── bootstrap_tf_backend.sh   # one-time: create state bucket + lock table
├── tf.sh                     # thin terraform wrapper (passes _backend.hcl)
├── wait_health.sh            # poll Temporal + vLLM /health using tf outputs
├── batch/
│   ├── up.sh                 # shared/platform + shared/temporal + shared/vllm + batch + wait
│   ├── submit.sh             # validate manifest, upload PDFs, upload manifest LAST
│   └── down.sh               # shared/vllm + batch + shared/temporal destroy (platform untouched)
├── live/
│   ├── up.sh                 # shared/platform + shared/temporal + shared/vllm + live + wait
│   ├── submit.sh             # upload PDFs to live/incoming/ (S3 -> SQS fires)
│   └── down.sh               # shared/vllm + live + shared/temporal destroy (platform untouched)
└── dev/
    ├── up_vllm.sh            # shared/vllm only, env_tag=dev
    └── down_vllm.sh
```
