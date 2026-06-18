variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-west-2"
}

variable "name_prefix" {
  description = "Prefix on every resource name."
  type        = string
  default     = "ocr-bench"
}

variable "tags" {
  description = "Extra tags. Project/Env are added automatically."
  type        = map(string)
  default     = {}
}

variable "env_tag" {
  description = "dev | prod. Tagged on the instance as role=vllm-<model>-<env_tag> so dev and prod boxes coexist unambiguously in the resolver."
  type        = string
  default     = "prod"
  validation {
    condition     = contains(["dev", "prod"], var.env_tag)
    error_message = "env_tag must be dev or prod."
  }
}

variable "model_key" {
  description = "OCR (vision) model identifier. Becomes the primary role tag (role=vllm-<model_key>-<env_tag>)."
  type        = string
  default     = "chandra"
}

variable "vllm_port" {
  description = "Port the vision vLLM unit listens on. Tagged onto the resolver URL (vllm-instance://<model_key>:<port>/...)."
  type        = number
  default     = 8004
}

variable "hf_model_id" {
  description = "Hugging Face model ID for the vision (OCR) `vllm serve` unit."
  type        = string
  default     = "datalab-to/chandra-ocr-2"
}

variable "vllm_extra_args" {
  description = "Extra args appended to the vision `vllm serve` command. Per-model tuning lives here."
  type        = string
  default     = ""
}

variable "vision_max_model_len" {
  description = "vision vLLM --max-model-len. OCR prompts are short; 8192 covers chandra comfortably."
  type        = number
  default     = 8192
}

variable "vision_gpu_memory_utilization" {
  description = "Fraction of total GPU memory the vision unit may use. Co-hosted with the tree_llm unit, so vision + tree_llm + headroom must sum to <= 1.0. At 0.38 + tree_llm 0.50 = 0.88 reserved, ~6 GiB box headroom on a 48 GB L40S — fits vLLM 0.23's compile-time transient (~5-10 GiB above declared utilization) without crowding."
  type        = number
  default     = 0.38
}

# --- co-hosted tree_llm (text) vLLM unit ------------------------------------
# Single GPU box, second vllm serve process on a different port. The instance
# is double-tagged (role + tree_llm_role) so shared/vllm/resolve.py can find
# the same box under both names.

variable "tree_llm_model_key" {
  description = "tree_llm model identifier. Becomes the secondary role tag (tree_llm_role=vllm-<tree_llm_model_key>-<env_tag>)."
  type        = string
  default     = "gemma"
}

variable "tree_llm_port" {
  description = "Port the tree_llm vLLM unit listens on. Must differ from vllm_port."
  type        = number
  default     = 8005

  validation {
    condition     = var.tree_llm_port != var.vllm_port
    error_message = "tree_llm_port must differ from vllm_port (two vllm serve processes can't share a port)."
  }
}

variable "tree_llm_hf_model_id" {
  description = "Hugging Face model ID for the tree_llm `vllm serve` unit. Default google/gemma-4-E4B-it: 8B total / 4.5B effective params via Per-Layer Embeddings, BF16 ~16GB on disk (the laptop ollama `gemma4:e4b` is the int4-quantized variant — different footprint). The `-it` (instruction-tuned) variant ships a chat template in its tokenizer; the base `google/gemma-4-E4B` does not, and transformers ≥4.44 refuses /v1/chat/completions on tokenizers without one. No HF gating."
  type        = string
  default     = "google/gemma-4-E4B-it"
}

variable "tree_llm_extra_args" {
  description = "Extra args appended to the tree_llm `vllm serve` command. Empty by default: BF16. FP8 (`--quantization fp8`) was attempted on gemma-4-E4B-it and failed at engine init — vLLM 0.23.0's FP8 path doesn't yet handle Gemma4's heterogeneous head dims (head_dim=256 local / 512 global). Revisit when vLLM lands FP8 for Gemma4ForConditionalGeneration."
  type        = string
  default     = ""
}

variable "tree_llm_max_model_len" {
  description = "tree_llm vLLM --max-model-len. 24K covers `generate_toc_continue` chunks (~18K chunk + accumulated prior tree) + 4K response with ~50% margin over the original 16385-token failure case. 32K does not co-host with chandra on a single L40S: vLLM's compile-time transient (~10 GiB above declared utilization) plus gemma's 0.55 slice crowds chandra out at startup. If 24K hits a coverage gate, the path is g6e.12xlarge multi-GPU split, not bumping back to 32K on this hardware."
  type        = number
  default     = 24576
}

variable "tree_llm_gpu_memory_utilization" {
  description = "Fraction of total GPU memory the tree_llm unit may use. vision + tree_llm + headroom must sum to <= 1.0. Sized for gemma-4-E4B BF16 (~16 GB weights) + 24K KV cache on a 48 GB L40S — the 0.50 slice leaves ~8 GB for the KV pool (≈3 concurrent 24K seqs). Combined with vision_gpu_memory_utilization=0.38 reserves 42.3 GiB / 48 GiB, leaving ~6 GiB box-level headroom for vLLM's compile-time transient (cudagraph capture + weight-load buffers run ~5-10 GiB above declared utilization on 0.23.x). The previous 0.55 + 32K combo crowded chandra out at startup — do not bump back without also moving off single-GPU."
  type        = number
  default     = 0.50
}

variable "instance_type" {
  description = "GPU instance type. Default g6e.xlarge = 1× L40S 48GB; fits co-hosted chandra (~17 GB peak) + gemma-4-E4B BF16 at 24K (~24 GB at 0.50) with ~6 GB box headroom for vLLM compile transient. 32K context for gemma does not co-host on a single L40S — vLLM 0.23 transient + gemma slice crowds chandra out. Bumping within g6e family below 12xlarge does NOT help (same single L40S, just more CPU/RAM). Real GPU bumps for 32K+: g6e.12xlarge (4× L40S, multi-GPU split — requires CUDA_VISIBLE_DEVICES per systemd unit) or p5.xlarge (1× H100 80GB)."
  type        = string
  default     = "g6e.xlarge"
}

variable "root_volume_gb" {
  description = "Root EBS. Sized for model weights (~20GB) + workspace + headroom."
  type        = number
  default     = 100
}

variable "vpc_id" {
  description = "VPC ID. null = default VPC. For prod, set to shared/temporal's vpc_id output so workers can reach the box privately."
  type        = string
  default     = null
}

variable "subnet_id" {
  description = "Subnet. null = first subnet of the selected VPC (or, if availability_zone is set, first subnet in that AZ)."
  type        = string
  default     = null
}

variable "availability_zone" {
  description = "AZ-name shortcut (e.g. \"us-west-2a\"). Picks the default-VPC subnet in that AZ. Use to dodge AZ capacity stalls — g6e in particular is thin in us-west-2c. Ignored when subnet_id is set."
  type        = string
  default     = null
}

variable "operator_cidrs" {
  description = "CIDRs allowed to reach the vLLM ports. Empty = no operator ingress (fail-closed); in-VPC workers reach vLLM via SG-to-SG ingress rules owned by the consumer modules (prod/batch, prod/live). bin/<motif>/up.sh and bin/dev/up_vllm.sh auto-detect the operator's public IP via checkip.amazonaws.com when this is not supplied — direct `terraform apply` callers must pass it explicitly."
  type        = list(string)
  default     = []
}

variable "key_pair_name" {
  description = "EC2 key pair. Required for prod debug ssh; Session Manager preferred."
  type        = string
  default     = null
}

variable "allow_ssh_from_operator" {
  description = "Add SSH ingress from operator_cidrs. Off by default; Session Manager is the supported access path."
  type        = bool
  default     = false
}

variable "ami_id" {
  description = "Override AMI. null = lookup latest Deep Learning Base GPU AMI (NVIDIA, Ubuntu 22.04) in this region."
  type        = string
  default     = null
}

variable "gpu_metrics_interval_s" {
  description = "Period of the nvidia-smi → CloudWatch sidecar. 30s aligns with CWAgent worker resolution."
  type        = number
  default     = 30
}

variable "gpu_metrics_namespace" {
  description = "CloudWatch namespace for the GPU sidecar. Default matches `prod/reports/hardware.py::GPU_NAMESPACE`."
  type        = string
  default     = "OCR/vLLM/GPU"
}
