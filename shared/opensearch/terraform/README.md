# shared/opensearch/terraform — single-node OpenSearch on EC2 spot

Cheapest reasonable backing store for the BM25 + dense hybrid index. Provisions
one t4g.medium Spot instance running OpenSearch 2.x in Docker, with an S3
snapshot bucket wired in.

Steady-state cost (us-west-2, mid-2026 pricing):

| Component        | Cost/mo    |
|------------------|------------|
| t4g.medium Spot  | ~$8        |
| gp3 root (30 GB) | ~$3        |
| S3 snapshots     | pennies    |
| **total**        | **~$11**   |

On-demand doubles-and-a-half that (~$30/mo). If losing the box to a Spot
interruption is unacceptable in your workflow, flip `use_spot = false` and
budget the on-demand rate.

## When to prefer this over Amazon OpenSearch Service

- Research / benchmarking / dev — corpus <5M chunks, single reader.
- Willing to snapshot to S3 + restore after interruptions.
- Fits the "we terraform our own Temporal cluster" MO already in this repo.

Reach for **Amazon OpenSearch Service managed** (~$50/mo minimum) when you want
zero ops: managed patching, automatic snapshots, in-place upgrades. The Python
client + index mapping in `shared/opensearch/` work against both — you only
change `pipeline_config.yaml:retrieval.opensearch.endpoint` and switch
`auth: basic` to `auth: aws_sigv4`.

Skip Amazon OpenSearch Serverless unless you have real bursty traffic — it
floors at ~$700/mo for 2+2 OCUs.

## Apply

```bash
cd shared/opensearch/terraform
terraform init -backend-config=../../terraform/_backend.hcl
terraform apply \
  -var 'operator_cidrs=["1.2.3.4/32"]' \
  -var 'worker_security_group_ids=["sg-xxxxxxxx"]'
```

Or drive it from a wrapper in `bin/` alongside the other stacks.

## Wiring workers

After apply, capture outputs into the pipeline config's overlay:

```yaml
retrieval:
  opensearch:
    endpoint: "https://10.x.x.x:9200"      # from `terraform output endpoint`
    auth: "basic"
    user_env: "OPENSEARCH_USER"
    password_env: "OPENSEARCH_PASSWORD"
    verify_certs: false                    # self-signed
    index_name: "chem-lit-chunks-v1"
```

And on each worker:

```bash
export OPENSEARCH_USER=admin
export OPENSEARCH_PASSWORD="$(aws ssm get-parameter \
  --name /ocr-bench/opensearch/admin_password \
  --with-decryption --query 'Parameter.Value' --output text)"
```

Systemd units for `prod/live/worker.service` should do the same in
`ExecStartPre`.

## Snapshots

The user_data script registers a `s3-primary` snapshot repository pointing at
the module-managed bucket. Trigger a snapshot with:

```bash
curl -sk -u "admin:$OPENSEARCH_PASSWORD" \
  -X PUT 'https://<endpoint>:9200/_snapshot/s3-primary/snap-YYYYMMDD?wait_for_completion=true'
```

Automate via cron on the box, or by hand before risky reindex operations.

## Known limits

- **Single node** — no replicas, no HA. Loss of the EBS volume = loss of the
  index. Restore from the latest snapshot.
- **Self-signed TLS** — `verify_certs: false` in the client config. To use a
  real cert, front the box with an ALB (adds ~$18/mo and IAM/ACM setup) or
  move to Amazon OpenSearch Service.
- **JVM heap = 1 GB** — hard-coded in the user_data. Bump `-Xms/-Xmx` when
  bumping the instance type; keep heap ≤ 50% of instance RAM.
- **No auth rotation** — the admin password lives in SSM. Rotate manually by
  re-running `terraform apply` with the `random_password` resource tainted.
