# `common/vllm`

vLLM EC2 box + SG. Independently apply-able from `common/temporal` so the
hybrid local-dev escape hatch (`bin/dev/up_vllm.sh`) can stand up just the
GPU box while a Mac drives the rest of the pipeline locally.

**Skeleton only — populated in Phase B of `AWS_DEPLOYMENT_PLAN.md`.** Once
`vllm.tf` + `variables.tf` (with `env_tag`, `instance_type`, `model_key`) +
`outputs.tf` (private + public IP, SG ID) land, this module replaces
`vllm/aws/launch.sh`. The `env_tag=dev|prod` knob lets a dev box and a prod
box coexist without ambiguity in the EC2-tag-based resolver.
