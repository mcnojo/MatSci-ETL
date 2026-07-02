# `shared/vllm/terraform`

vLLM EC2 boxes + SGs — one instance per entry in `var.models`. Independently
apply-able from `shared/temporal/` so the two motifs (batch, live) can share
the same GPU fleet without cross-module dependency.

| Input          | Purpose                                                                            |
| -------------- | ---------------------------------------------------------------------------------- |
| `models`       | Map keyed by primary role_key. Per-entry: primary vLLM config plus optional `secondary_services` for co-hosted extra roles on the same box |
| `availability_zone` | AZ shortcut applied to every instance (dodge capacity stalls)                 |
| `operator_cidrs` | CIDRs allowed to reach each service's port (per-service SG ingress)             |

Defaults provision two asymmetric boxes:
- `chandra` on `g6.xlarge` (L4 24GB) — primary port 8004 (OCR); secondary port
  8006 hosts `BAAI/bge-m3` (embeddings). Chandra's ~17GB peak + bge-m3's ~2.2GB
  fp16 fits under the 24GB L4 with the module's `sum(gpu_memory_utilization) ≤ 0.95`
  invariant enforced at plan time.
- `gemma`   on `g6e.xlarge` (L40S 48GB), port 8005 — dedicated tree_llm at 128K context

Each box runs one `vllm serve` systemd unit per served role. The resolver
(`shared/vllm/resolve.py`) finds a box by the `vllm_role_<key>=true` EC2 tag —
each box tags itself with one such key per role it serves. So
`vllm-instance://embed:8006/v1` and `vllm-instance://chandra:8004/v1` both
resolve to the chandra box IP, hitting different ports; `vllm-instance://
gemma:8005/v1` resolves to the gemma box.

The `models` output is a map keyed by the primary role_key; each value
carries `instance_id`, `public_ip`, `private_ip`, `security_group_id`,
`instance_type`, and `services` (list of `{role_key, role_tag, hf_model_id,
port}` for every co-hosted role). Consumers iterate.

Adding a third box is a one-line edit to `var.models` — `for_each` plumbs
the rest (instance, SG, IAM role/profile, per-service ingress rules,
user_data render).

Adding a co-hosted role on an existing box: append to that entry's
`secondary_services` list. Plan-time validators enforce distinct ports,
distinct role_keys, and per-box `sum(gpu_memory_utilization) ≤ 0.95`.
