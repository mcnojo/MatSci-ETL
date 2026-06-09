# `common/vllm`

vLLM EC2 box + SG. Independently apply-able from `common/temporal` so the
hybrid local-dev escape hatch (`bin/dev/up_vllm.sh`) can stand up just the
GPU box while a Mac drives the rest of the pipeline locally.

The `env_tag=dev|prod` knob lets a dev box and a prod box coexist without
ambiguity in the EC2-tag-based resolver (`shared/vllm_resolve.py` filters on
`role=vllm-<model>-<env_tag>`).

| Input        | Purpose                                                          |
| ------------ | ---------------------------------------------------------------- |
| `env_tag`    | `dev` or `prod` — tagged onto the instance for resolver scoping  |
| `model_key`  | Which OCR model (chandra default). Part of the role tag.         |
| `hf_model_id`| Hugging Face model ID `vllm serve` loads at boot                 |
| `instance_type` | GPU box sizing — default `g6.xlarge` (1× L4 24GB)             |

Outputs the public IP (operator-facing) and private IP (in-VPC workers route
here when `OCR_VLLM_PREFER_PRIVATE_IP=1` is set on the worker box).
