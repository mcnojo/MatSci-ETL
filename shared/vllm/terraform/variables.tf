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

# Each entry provisions one dedicated GPU instance running exactly one
# `vllm serve` unit. Key becomes the role tag (role=vllm-<key>) and
# the host portion of vllm-instance:// URLs the resolver consumes.
#
# Defaults split chandra (L4, modest context) and gemma (L40S, 128K context)
# onto their own hardware. Co-hosting on a single L40S was the previous shape;
# it crowded gemma's KV pool below what `generate_toc_continue` needs.
variable "models" {
  description = "Per-model GPU instances. Map keyed by model_key; each value pins the instance type, vLLM port, model id, and per-process GPU/context sizing."
  type = map(object({
    instance_type          = string
    hf_model_id            = string
    port                   = number
    max_model_len          = number
    gpu_memory_utilization = number
    extra_args             = string
  }))
  default = {
    chandra = {
      instance_type          = "g6.xlarge" # 1× L4 24 GB. chandra peak ~17 GB at 8K — fits with room.
      hf_model_id            = "datalab-to/chandra-ocr-2"
      port                   = 8004
      max_model_len          = 8192 # OCR prompts are short; 8K is plenty.
      gpu_memory_utilization = 0.85 # dedicated box: take the GPU.
      extra_args             = ""
    }
    gemma = {
      instance_type          = "g6e.xlarge" # 1× L40S 48 GB. gemma BF16 ~16 GB + 128K KV fits at 0.85.
      hf_model_id            = "google/gemma-4-E4B-it"
      port                   = 8005
      max_model_len          = 131072 # full 128K — kills `generate_toc_continue` truncation.
      gpu_memory_utilization = 0.85
      extra_args             = "" # FP8 still blocked on Gemma4 heterogeneous head_dim (vLLM 0.23.0).
    }
  }

  # Ports must be globally unique so wait_health, resolver-derived URLs, and
  # human debugging never collide. Cheap plan-time guard.
  validation {
    condition     = length(var.models) == length(distinct([for m in values(var.models) : m.port]))
    error_message = "models.*.port values must all be distinct."
  }

  validation {
    condition     = alltrue([for m in values(var.models) : m.gpu_memory_utilization > 0 && m.gpu_memory_utilization <= 0.95])
    error_message = "gpu_memory_utilization must be in (0, 0.95]; >0.95 leaves no headroom for vLLM's compile-time transient."
  }
}

variable "root_volume_gb" {
  description = "Root EBS for each GPU box. Sized for model weights (~20 GB) + workspace + headroom."
  type        = number
  default     = 100
}

variable "vpc_id" {
  description = "VPC ID. null = default VPC. For prod, set to shared/temporal's vpc_id output so workers can reach the boxes privately."
  type        = string
  default     = null
}

variable "subnet_id" {
  description = "Subnet. null = first subnet of the selected VPC (or, if availability_zone is set, first subnet in that AZ). Applies to every instance — all models share one AZ."
  type        = string
  default     = null
}

variable "availability_zone" {
  description = "AZ-name shortcut (e.g. \"us-west-2a\"). Picks the default-VPC subnet in that AZ. Use to dodge AZ capacity stalls — g6e in particular is thin in us-west-2c. Ignored when subnet_id is set. Applies to every model in this module."
  type        = string
  default     = null
}

variable "operator_cidrs" {
  description = "CIDRs allowed to reach each vLLM port. Empty = no operator ingress (fail-closed); in-VPC workers reach vLLM via SG-to-SG ingress rules owned by the consumer modules (prod/batch, prod/live). bin/<motif>/up.sh auto-detects the operator's public IP via checkip.amazonaws.com when this is not supplied — direct `terraform apply` callers must pass it explicitly."
  type        = list(string)
  default     = []
}

variable "key_pair_name" {
  description = "EC2 key pair. Required for prod debug ssh; Session Manager preferred."
  type        = string
  default     = null
}

variable "allow_ssh_from_operator" {
  description = "Add SSH ingress from operator_cidrs on every box. Off by default; Session Manager is the supported access path."
  type        = bool
  default     = false
}

variable "ami_id" {
  description = "Override AMI. null = lookup latest Deep Learning Base GPU AMI (NVIDIA, Ubuntu 22.04) in this region."
  type        = string
  default     = null
}

variable "gpu_metrics_interval_s" {
  description = "Period of the nvidia-smi -> CloudWatch sidecar. 30s aligns with CWAgent worker resolution."
  type        = number
  default     = 30
}

variable "gpu_metrics_namespace" {
  description = "CloudWatch namespace for the GPU sidecar. Default matches `prod/reports/hardware.py::GPU_NAMESPACE`."
  type        = string
  default     = "OCR/vLLM/GPU"
}
