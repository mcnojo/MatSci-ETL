# `shared/vllm/terraform`

vLLM EC2 box + SG. Independently apply-able from `shared/temporal/` so the
hybrid local-dev escape hatch (`bin/dev/up_vllm.sh`) can stand up just the
GPU box while a Mac drives the rest of the pipeline locally.

The `env_tag=dev|prod` knob lets a dev box and a prod box coexist without
ambiguity in the EC2-tag-based resolver (`shared/vllm/resolve.py` filters on
`role=vllm-<model>-<env_tag>`).

| Input        | Purpose                                                          |
| ------------ | ---------------------------------------------------------------- |
| `env_tag`    | `dev` or `prod` — tagged onto the instance for resolver scoping  |
| `model_key`  | Vision (OCR) model identifier. Primary role tag.                 |
| `hf_model_id`| HF model ID the vision `vllm serve` unit loads                   |
| `tree_llm_model_key` | tree_llm model identifier. Secondary role tag.           |
| `tree_llm_hf_model_id` | HF model ID the tree_llm `vllm serve` unit loads       |
| `instance_type` | GPU box sizing — default `g6e.xlarge` (1× L40S 48GB), fits both models co-hosted with headroom |

Two vLLM units run on the same box:
- vision (OCR): port `vllm_port` (default 8004), tagged `role=vllm-<model_key>-<env_tag>`
- tree_llm:     port `tree_llm_port` (default 8005), tagged `tree_llm_role=vllm-<tree_llm_model_key>-<env_tag>`

Each process gets its slice of GPU memory via `vision_gpu_memory_utilization` /
`tree_llm_gpu_memory_utilization`. The resolver (`shared/vllm/resolve.py`) walks
both tag keys so callers write `vllm-instance://chandra:8004/v1` and
`vllm-instance://gemma:8005/v1` against the same box.

Outputs the public IP (operator-facing) and private IP (in-VPC workers route
here when `OCR_VLLM_PREFER_PRIVATE_IP=1` is set on the worker box).
