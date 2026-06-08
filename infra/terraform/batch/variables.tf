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

variable "cpu_queue_instance_types" {
  description = "Mixed-instances override list for the cpu-task-queue fleet. Sized for doclayout-yolo + PyMuPDF under bounded torch threads (OMP=2)."
  type        = list(string)
  default     = ["c7i.xlarge", "m7i.xlarge", "c7i.large"]
}

variable "gpu_queue_instance_types" {
  description = "Mixed-instances override list for the gpu-task-queue fleet. The work is HTTP IO to vLLM, so CPU sizes suffice (no local GPU)."
  type        = list(string)
  default     = ["c7i.large", "m7i.large", "c5.large"]
}

variable "cpu_queue_max_size" {
  description = "cpu-task-queue ASG ceiling. Bounded by Standard Spot vCPU quota."
  type        = number
  default     = 2
}

variable "gpu_queue_max_size" {
  description = "gpu-task-queue ASG ceiling. Also bounded by Standard Spot vCPU quota (no G/VT quota — see gpu_queue_instance_types)."
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
  description = "Per-instance max_concurrent_activities on cpu-task-queue. Sized for c7i.xlarge × OMP=2 (8 threads on 4 cores)."
  type        = number
  default     = 4
}

variable "max_concurrent_gpu" {
  description = "Per-instance max_concurrent_activities on gpu-task-queue. Bottleneck is vLLM, not the proxy — raise to push more vLLM throughput."
  type        = number
  default     = 8
}

variable "torch_num_threads" {
  description = "Caps torch/OMP/MKL threads per activity to prevent N×N thread oversubscription under max_concurrent_cpu."
  type        = number
  default     = 2
}

variable "artifact_bucket" {
  description = "S3 bucket for pipeline artifacts and batch reports."
  type        = string
}

variable "tree_llm_ssm_prefix" {
  description = "SSM parameter prefix for tree_llm API keys. Workers fetch on boot into /etc/ocr-benchmarking/tree_llm.env."
  type        = string
  default     = "/ocr-bench/tree_llm"
}

variable "lifecycle_hook_heartbeat_s" {
  description = "Drain window before lifecycle hook defaults to CONTINUE. >= WORKER_GRACEFUL_SHUTDOWN_TIMEOUT."
  type        = number
  default     = 120
}
