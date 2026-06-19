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

variable "cpu_queue_instance_types" {
  description = "Mixed-instances override list for the batch-cpu-tq fleet. Sized for doclayout-yolo + PyMuPDF under bounded torch threads (OMP=2)."
  type        = list(string)
  default     = ["c7i.xlarge", "m7i.xlarge", "c7i.large"]
}

variable "gpu_queue_instance_types" {
  description = "Mixed-instances override list for the batch-gpu-tq fleet. The work is HTTP IO to vLLM, so CPU sizes suffice (no local GPU)."
  type        = list(string)
  default     = ["c7i.large", "m7i.large", "c5.large"]
}

variable "cpu_queue_max_size" {
  description = "batch-cpu-tq ASG ceiling. Bounded by Standard Spot vCPU quota."
  type        = number
  default     = 2
}

variable "gpu_queue_max_size" {
  description = "batch-gpu-tq ASG ceiling. Also bounded by Standard Spot vCPU quota (no G/VT quota — see gpu_queue_instance_types)."
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

variable "temporal_namespace" {
  description = "Temporal namespace."
  type        = string
  default     = "default"
}

variable "max_concurrent_cpu" {
  description = "Per-instance max_concurrent_activities on batch-cpu-tq. Sized for c7i.xlarge × OMP=2 (8 threads on 4 cores)."
  type        = number
  default     = 4
}

variable "max_concurrent_gpu" {
  description = "Per-instance max_concurrent_activities on batch-gpu-tq. Bottleneck is vLLM, not the proxy — raise to push more vLLM throughput."
  type        = number
  default     = 8
}

variable "torch_num_threads" {
  description = "Caps torch/OMP/MKL threads per activity to prevent N×N thread oversubscription under max_concurrent_cpu."
  type        = number
  default     = 2
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

variable "root_volume_gb" {
  description = "Root EBS volume size. Must fit AL2023 base + dnf cache + pip install of torch + nvidia-* CUDA wheels (~3-4GB combined via doclayout-yolo's torch dep) + opencv-python-headless + transformers. 30GB ran out at install time; 50GB leaves headroom over the ~12GB peak observed."
  type        = number
  default     = 50
}

variable "log_collection_enabled" {
  description = "When true, each worker installs + runs the CloudWatch agent and ships /var/log/ocr-batch-worker.log to the batch_worker log group. When false, the agent is never installed and logs stay local on the worker (readable for the worker's lifetime via SSM until it terminates). Only takes effect on instance creation -- flipping rerolls the launch template which triggers an ASG instance refresh."
  type        = bool
  default     = true
}

variable "worker_registration_timeout_s" {
  description = "BatchRunInput.worker_registration_timeout_s — bound on await_pollers_activity. Plumbed into the CLI via terraform outputs. Cold-start budget: spot fulfill (~30s) + dnf update + install (~3 min) + pip install heavy CUDA wheels (~3-4 min) + worker boot (~15s). 40 min ceiling absorbs Spot variability, slow PyPI tail, and headroom for vLLM model swap on the GPU box (chandra -> gemma)."
  type        = number
  default     = 2400
}

# Remote-state coordinates — defaults match shared/terraform/_backend.hcl.
variable "state_bucket" {
  description = "S3 bucket holding shared/temporal's tfstate."
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
