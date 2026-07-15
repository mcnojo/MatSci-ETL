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

# 5. HF Hub read token — used by bin/stage_model.sh only (vLLM boxes never
#    talk to the Hub; they read weights offline from S3). Get one at
#    https://huggingface.co/settings/tokens (read-only is enough).
aws ssm put-parameter --overwrite --type SecureString \
    --name /ocr-bench/hf_token --value "hf_..."
```

---

## Staging model weights

vLLM boxes serve weights offline from S3; `bin/<motif>/up.sh` calls
`bin/stage_model.sh --all` before applying `shared/vllm`. For ad-hoc stage
/ list / delete commands (and the concrete `gemma-4-12b-it` walkthrough)
see `docs/how_to.md` §0.6 and §2.5.

---

## Normal usage

### Batch

```bash
bin/batch/up.sh                                   # ~5 min cold (S3 sync); first-ever stage is +20-30 min
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
├── stage_model.sh            # launches ephemeral EC2 -> hf download -> aws s3 sync -> .done -> self-terminate
├── delete_model.sh           # remove staged weights from S3 (--list / --all / <id> [<rev>])
├── wait_health.sh            # poll Temporal + vLLM /health using tf outputs
├── vllm_diag.sh              # SSM fan-out: user-data + vllm.log + unit state + listening ports
├── batch/
│   ├── up.sh                 # shared/platform + shared/temporal + shared/vllm (stage + apply) + batch + wait
│   ├── submit.sh             # validate manifest, upload PDFs, upload manifest LAST
│   └── down.sh               # shared/vllm + batch + shared/temporal destroy (platform untouched)
└── live/
    ├── up.sh                 # shared/platform + shared/temporal + shared/vllm (stage + apply) + live + wait
    ├── submit.sh             # upload PDFs to live/incoming/ (S3 -> SQS fires)
    └── down.sh               # shared/vllm + live + shared/temporal destroy (platform untouched)
```
