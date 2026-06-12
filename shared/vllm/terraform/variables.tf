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
  description = "Fraction of total GPU memory the vision unit may use. Co-hosted with the tree_llm unit, so vision + tree_llm + headroom must sum to <= 1.0."
  type        = number
  default     = 0.38
  # Sized assuming gemma-4-E4B served at BF16 (~16GB weights — the HF default
  # ships full BF16 even though effective params are 4.5B via Per-Layer
  # Embeddings). If you switch tree_llm to FP8 or AWQ via tree_llm_extra_args,
  # bump this up to 0.50+.
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
  description = "Hugging Face model ID for the tree_llm `vllm serve` unit. Default google/gemma-4-E4B: 8B total / 4.5B effective params via Per-Layer Embeddings, BF16 ~16GB on disk (the laptop ollama `gemma4:e4b` is the int4-quantized variant — different footprint). No HF gating."
  type        = string
  default     = "google/gemma-4-E4B"
}

variable "tree_llm_extra_args" {
  description = "Extra args appended to the tree_llm `vllm serve` command."
  type        = string
  default     = ""
}

variable "tree_llm_max_model_len" {
  description = "tree_llm vLLM --max-model-len. Tree extraction prompts can include 10-20 pages; 16K balances coverage vs KV cache on gemma-4 8B."
  type        = number
  default     = 16384
}

variable "tree_llm_gpu_memory_utilization" {
  description = "Fraction of total GPU memory the tree_llm unit may use. vision + tree_llm + headroom must sum to <= 1.0. Sized for gemma-4-E4B at BF16 (~16GB weights + KV cache) on a 48GB L40S."
  type        = number
  default     = 0.45
}

variable "instance_type" {
  description = "GPU instance type. Default g6e.xlarge = 1× L40S 48GB; required headroom for co-hosting chandra (~17GB peak) + gemma-4-E4B at BF16 (~20GB peak). g6.xlarge (L4 24GB) does not fit BF16 cohost — would require FP8/int4 quantization via tree_llm_extra_args."
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
  description = "CIDRs allowed to reach the vLLM port. Default: world-open for hybrid dev (operator's Mac). Tighten to the operator's IP CIDR for prod."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "worker_security_group_ids" {
  description = "SGs of in-VPC workers that need to call vLLM. Each gets an ingress rule on the vLLM port. Empty for hybrid local-dev."
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
