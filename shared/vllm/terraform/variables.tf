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

# Each entry provisions one dedicated GPU instance. The primary vLLM service
# uses the entry-level fields; optional `secondary_services` add extra vLLM
# processes co-hosted on the same box (each its own systemd unit, port, GPU
# budget). Every role served — primary + secondaries — surfaces as a
# `vllm_role_<key> = true` EC2 tag, so the resolver can point
# `vllm-instance://<role>:<port>/...` at the box regardless of whether that
# role is the primary or a co-host.
#
# The chandra box carries an embedding secondary (bge-m3) because chandra
# peaks at ~17 GB / L4-24 GB and has real headroom; the workflow's embed
# activity resolves `vllm-instance://embed:...` to the same instance IP,
# hitting a distinct port.
#
# Weights come from S3 (bin/stage_model.sh stages them) — user_data syncs
# s3://<bucket>/models/<hf_id>/<revision>/ → /opt/models/<key>/ and serves
# with HF_HUB_OFFLINE=1. Missing .done marker fails boot loudly.
variable "models" {
  description = "Per-instance vLLM deployment. Map keyed by primary role_key; each value pins the instance type + primary vLLM config, plus optional co-hosted secondaries."
  type = map(object({
    instance_type          = string
    hf_model_id            = string
    hf_revision            = string # S3 subdir. Free-form ("main", SHA, ...); must match a prior bin/stage_model.sh run.
    port                   = number
    max_model_len          = number
    gpu_memory_utilization = number
    extra_args             = string
    secondary_services = optional(list(object({
      key                    = string
      hf_model_id            = string
      hf_revision            = string
      port                   = number
      max_model_len          = number
      gpu_memory_utilization = number
      extra_args             = string
    })), [])
  }))
  default = {
    chandra = {
      instance_type          = "g6.xlarge" # 1× L4 24 GB. chandra ~17 GB peak at 8K; embed secondary fits alongside.
      hf_model_id            = "datalab-to/chandra-ocr-2"
      hf_revision            = "main"
      port                   = 8004
      max_model_len          = 8192
      gpu_memory_utilization = 0.75 # room for bge-m3 co-host below.
      extra_args             = ""
      secondary_services = [
        {
          key                    = "embed"       # resolves `vllm-instance://embed:8006/*` to this box.
          hf_model_id            = "BAAI/bge-m3" # 568M params, 1024-dim, strong on scientific text.
          hf_revision            = "main"
          port                   = 8006
          max_model_len          = 8192                            # bge-m3 stops attending past 8K; larger wastes KV.
          gpu_memory_utilization = 0.15                            # ~3.6 GB on L4 24 — bge-m3 fp16 ~2.2 GB + margin.
          extra_args             = "--runner pooling --dtype auto" # --served-model-name set by user_data.
        },
      ]
    }
    gemma = {
      instance_type          = "g6e.xlarge" # 1× L40S 48 GB. gemma-4-12b BF16 ~24 GB weights; sliding-window KV holds 128K at 0.90 util.
      hf_model_id            = "google/gemma-4-12b-it"
      hf_revision            = "main"
      port                   = 8005
      max_model_len          = 131072 # full 128K — kills `generate_toc_continue` truncation. If boot OOMs on KV, drop to 32768.
      gpu_memory_utilization = 0.90
      # --max-num-seqs 2 doubles per-request tok/s vs 4-way default; prefix
      # caching reuses the shared system/schema header across tree-walk calls.
      # FP8 still blocked on Gemma4 heterogeneous head_dim (vLLM 0.23.0).
      extra_args             = "--max-num-seqs 2 --enable-prefix-caching"
    }
  }

  # Every port across every service (primary + secondary) must be globally
  # unique so wait_health, resolver-derived URLs, and human debugging never
  # collide. Cheap plan-time guard.
  validation {
    condition = length(distinct(flatten([
      for k, m in var.models : concat(
        [m.port],
        [for svc in m.secondary_services : svc.port],
      )
      ]))) == length(flatten([
      for k, m in var.models : concat(
        [m.port],
        [for svc in m.secondary_services : svc.port],
      )
    ]))
    error_message = "All vLLM ports (primary + secondary) must be distinct across every box."
  }

  # Every role_key across every service must be globally unique. Duplicates
  # would produce colliding EC2 tags (`vllm_role_<key>=true` on two boxes)
  # and the resolver would raise `multiple running instances tagged ...`.
  validation {
    condition = length(distinct(flatten([
      for k, m in var.models : concat(
        [k],
        [for svc in m.secondary_services : svc.key],
      )
      ]))) == length(flatten([
      for k, m in var.models : concat(
        [k],
        [for svc in m.secondary_services : svc.key],
      )
    ]))
    error_message = "All role keys (primary + secondary) must be distinct."
  }

  # Per-service utilization is in (0, 0.95]. Sum-per-box is also in (0, 0.95]
  # — leaves ≥5% for CUDA contexts and vLLM's compile transient outside its
  # own budget.
  validation {
    condition = alltrue([
      for k, m in var.models : (
        m.gpu_memory_utilization > 0 && m.gpu_memory_utilization <= 0.95 &&
        alltrue([for svc in m.secondary_services : svc.gpu_memory_utilization > 0 && svc.gpu_memory_utilization <= 0.95])
      )
    ])
    error_message = "Each service's gpu_memory_utilization must be in (0, 0.95]."
  }

  validation {
    condition = alltrue([
      for k, m in var.models : (
        m.gpu_memory_utilization
        + sum(concat([0], [for svc in m.secondary_services : svc.gpu_memory_utilization]))
      ) <= 0.95
    ])
    error_message = "Sum of gpu_memory_utilization per box (primary + secondaries) must be ≤ 0.95."
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

# Backend-config trio for terraform_remote_state -> shared/platform (weights
# bucket lookup). Mirrors prod/batch + prod/live; defaults match _backend.hcl.
variable "state_bucket" {
  description = "S3 bucket holding shared/platform's tfstate."
  type        = string
  default     = "ocr-benchmarking-tfstate"
}

variable "state_region" {
  description = "Region of state_bucket."
  type        = string
  default     = "us-west-2"
}

variable "state_lock_table" {
  description = "DynamoDB lock table for state_bucket."
  type        = string
  default     = "ocr-benchmarking-tflock"
}

