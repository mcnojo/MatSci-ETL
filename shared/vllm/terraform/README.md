# `shared/vllm/terraform`

vLLM EC2 boxes + SGs — one instance per entry in `var.models`. Independently
apply-able from `shared/temporal/` so the two motifs (batch, live) can share
the same GPU fleet without cross-module dependency.

| Input          | Purpose                                                                            |
| -------------- | ---------------------------------------------------------------------------------- |
| `models`       | Map keyed by model_key. Per-entry: `instance_type`, `hf_model_id`, `port`, `max_model_len`, `gpu_memory_utilization`, `extra_args` |
| `availability_zone` | AZ shortcut applied to every instance (dodge capacity stalls)                 |
| `operator_cidrs` | CIDRs allowed to reach each model's port (per-model SG ingress)                  |

Defaults provision two asymmetric boxes:
- `chandra` on `g6.xlarge` (L4 24GB), port 8004 — dedicated OCR
- `gemma`   on `g6e.xlarge` (L40S 48GB), port 8005 — dedicated tree_llm at 128K context

Each box runs a single `vllm serve` systemd unit. The resolver
(`shared/vllm/resolve.py`) finds it via `role=vllm-<key>`, so callers write
`vllm-instance://chandra:8004/v1` and `vllm-instance://gemma:8005/v1` — each
resolves to a different IP.

The `models` output is a map keyed by `model_key`; each value carries
`instance_id`, `public_ip`, `private_ip`, `port`, `role_tag`,
`security_group_id`, `instance_type`, `hf_model_id`. Consumers iterate.

Adding a third model is a one-line edit to `var.models` — `for_each` plumbs
the rest (instance, SG, IAM role/profile, ingress rule, user_data render).
