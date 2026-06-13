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

variable "vpc_id" {
  description = "VPC ID. null = default VPC."
  type        = string
  default     = null
}

variable "subnet_id" {
  description = "Subnet for cpu-pipeline-01. null = first subnet of the selected VPC."
  type        = string
  default     = null
}

variable "instance_type" {
  description = "cpu-pipeline-01 instance type. Hosts Temporal, Postgres, the always-on live worker (live-cpu-tq + live-gpu-tq), the batch control worker (batch-control-tq), and the live consumer."
  type        = string
  default     = "m7i.xlarge"
}

variable "root_volume_gb" {
  description = "Root EBS volume size."
  type        = number
  default     = 100
}

variable "operator_cidrs" {
  description = "CIDRs allowed inbound on Temporal UI (8233) + Temporal gRPC (7233) + SSH (22). Default: locked to nothing — use Session Manager for shell access; open to operator IP only when needed."
  type        = list(string)
  default     = []
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

variable "artifact_bucket" {
  description = "Existing S3 bucket for pipeline artifacts and batch reports."
  type        = string
  default     = "chem-lit-artifacts"
}

variable "tree_llm_ssm_prefix" {
  description = "SSM parameter prefix for tree_llm API keys. Created as empty SecureStrings — populate with `aws ssm put-parameter`."
  type        = string
  default     = "/ocr-bench/tree_llm"
}

variable "live_ssm_prefix" {
  description = "SSM parameter prefix the live motif uses to hand the SQS queue URL off to cpu-pipeline-01. Authoritative — live/ reads this via remote_state."
  type        = string
  default     = "/ocr-bench/live"
}

variable "max_concurrent_cpu" {
  description = "Per-process cap on live-cpu-tq + batch-control-tq activities for the local workers on cpu-pipeline-01."
  type        = number
  default     = 8
}

variable "max_concurrent_gpu" {
  description = "Per-process cap on live-gpu-tq activities for the local worker on cpu-pipeline-01."
  type        = number
  default     = 4
}

variable "torch_num_threads" {
  description = "Caps torch/OMP/MKL threads per activity."
  type        = number
  default     = 2
}
