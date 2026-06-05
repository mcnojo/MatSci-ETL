variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-west-2"
}

variable "name_prefix" {
  description = "Prefix on every resource name."
  type        = string
  default     = "ocr-batch"
}

variable "tags" {
  description = "Extra tags. Project/Env are added automatically."
  type        = map(string)
  default     = {}
}

variable "vpc_id" {
  description = "VPC ID. null = default VPC."
  type        = string
  default     = null
}

variable "subnet_ids" {
  description = "Subnets the ASGs may launch into. Empty = all subnets in the VPC (multi-AZ Spot diversity)."
  type        = list(string)
  default     = []
}

variable "cpu_pipeline_security_group_id" {
  description = "SG on cpu-pipeline-01. We add one ingress rule on 7233 from the worker SG."
  type        = string
}

variable "cpu_instance_types" {
  description = "CPU mixed-instances override list; order hints capacity-optimized."
  type        = list(string)
  default     = ["c7i.large", "c7i.xlarge", "m7i.large"]
}

variable "gpu_instance_types" {
  description = "GPU mixed-instances override list."
  type        = list(string)
  default     = ["g6.xlarge", "g6.2xlarge", "g5.xlarge"]
}

variable "cpu_max_size" {
  description = "CPU ASG ceiling. Bounded by Standard Spot vCPU quota."
  type        = number
  default     = 2
}

variable "gpu_max_size" {
  description = "GPU ASG ceiling. Bounded by G/VT Spot vCPU quota."
  type        = number
  default     = 2
}

variable "spot_allocation_strategy" {
  description = "Spot allocation strategy."
  type        = string
  default     = "capacity-optimized"

  validation {
    condition     = contains(["capacity-optimized", "price-capacity-optimized", "lowest-price"], var.spot_allocation_strategy)
    error_message = "Must be one of capacity-optimized, price-capacity-optimized, lowest-price."
  }
}

variable "key_pair_name" {
  description = "EC2 key pair for SSH. Optional — Session Manager is preferred."
  type        = string
  default     = null
}

variable "repo_url" {
  description = "Git URL the bootstrap clones."
  type        = string
  default     = "https://github.com/mcnojo/ocr-benchmarking.git"
}

variable "repo_ref" {
  description = "Git ref to check out. Pin a tag for reproducible rollouts."
  type        = string
  default     = "main"
}

variable "temporal_address" {
  description = "Temporal host:port. Use cpu-pipeline-01's private IP."
  type        = string
}

variable "temporal_namespace" {
  description = "Temporal namespace."
  type        = string
  default     = "default"
}

variable "max_concurrent_cpu" {
  description = "Per-instance max_concurrent_activities on cpu-task-queue."
  type        = number
  default     = 8
}

variable "max_concurrent_gpu" {
  description = "Per-instance max_concurrent_activities on gpu-task-queue."
  type        = number
  default     = 4
}

variable "artifact_bucket" {
  description = "S3 bucket for pipeline artifacts and batch reports."
  type        = string
}

variable "target_backlog_per_worker" {
  description = "Target tasks-per-worker for the scaling policy."
  type        = number
  default     = 4
}

variable "scale_in_cooldown_s" {
  description = "Scale-in cooldown (s)."
  type        = number
  default     = 300
}

variable "scale_out_cooldown_s" {
  description = "Scale-out cooldown (s)."
  type        = number
  default     = 60
}

variable "lifecycle_hook_heartbeat_s" {
  description = "Drain window before lifecycle hook defaults to CONTINUE. >= WORKER_GRACEFUL_SHUTDOWN_TIMEOUT."
  type        = number
  default     = 120
}
