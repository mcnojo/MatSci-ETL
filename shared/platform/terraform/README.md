# shared/platform

Long-lived account-scoped resources that are NEVER torn down by the daily
`bin/<motif>/down.sh` flow. Applied ONCE per account, then forgotten.

Currently owns:

- `aws_ssm_parameter.anthropic_api_key` — SecureString slot for the
  tree_llm Anthropic key. Created with a placeholder; operator populates the
  real value once via `aws ssm put-parameter --overwrite`.
- `aws_ssm_parameter.openai_api_key` — same, for the OpenAI key.

Both resources carry `lifecycle { prevent_destroy = true }` so an accidental
`terraform destroy` here cannot lose the populated key value.

## First-time setup

```bash
bin/tf.sh shared/platform init
bin/tf.sh shared/platform apply

aws ssm put-parameter \
    --name /ocr-bench/tree_llm/anthropic_api_key \
    --type SecureString --overwrite --value "sk-ant-..."
aws ssm put-parameter \
    --name /ocr-bench/tree_llm/openai_api_key \
    --type SecureString --overwrite --value "sk-..."
```

After this, `shared/temporal` and any other consumer reads the keys by name
via SSM at instance boot — no terraform-managed values, no rotation churn.

## Why a separate module

`shared/temporal` previously created and owned these slots, which meant
`bin/<motif>/down.sh` deleted them every night. The operator-populated key
values were lost with the slots; the next `up.sh` recreated empty
placeholders that silently 401'd against the providers until repopulated.
Splitting platform out of the compute lifecycle eliminates that footgun.
