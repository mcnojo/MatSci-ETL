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
  description = "Extra tags."
  type        = map(string)
  default     = {}
}

variable "vpc_id" {
  description = "VPC ID. null = default VPC. For prod, set to shared/temporal's vpc_id so workers reach the box privately."
  type        = string
  default     = null
}

variable "subnet_id" {
  description = "Subnet. null = first subnet in the selected VPC."
  type        = string
  default     = null
}

variable "instance_type" {
  description = "Single-node instance. t4g.medium runs OpenSearch 2.x + ~1M chunks comfortably; step up to t4g.large or c7g.large if the corpus grows past ~5M docs. ARM (t4g/c7g) is ~30% cheaper than x86 for equivalent perf on OS 2.x."
  type        = string
  default     = "t4g.medium"
}

variable "use_spot" {
  description = "Use a Spot instance for the OS box. Cheapest path (~$8/mo for t4g.medium). Trade-off: an interruption drops the box; snapshots to S3 buy back durability. Off = on-demand ($30/mo)."
  type        = bool
  default     = true
}

variable "root_volume_gb" {
  description = "Root EBS. OS 2.x + indices + snapshot staging: 30 GB fits ~500K chunks with room."
  type        = number
  default     = 30
}

variable "operator_cidrs" {
  description = "CIDRs allowed to reach OS on 9200. Empty = no operator ingress (fail-closed); in-VPC workers reach via SG-to-SG rules attached by consumer modules."
  type        = list(string)
  default     = []
}

variable "worker_security_group_ids" {
  description = "Worker SGs allowed to reach OS on 9200 via SG-to-SG ingress. Attach the cpu-pipeline SG from shared/temporal + the batch worker SG from prod/batch."
  type        = list(string)
  default     = []
}

variable "key_pair_name" {
  description = "EC2 key pair. Optional; Session Manager is the preferred access path."
  type        = string
  default     = null
}

variable "opensearch_version" {
  description = "OpenSearch Docker image tag. Pin to a specific 2.x so k-NN mapping settings don't shift under you."
  type        = string
  default     = "2.15.0"
}

variable "snapshot_bucket_name" {
  description = "S3 bucket for OpenSearch snapshots. Created if create_snapshot_bucket = true."
  type        = string
  default     = null
}

variable "create_snapshot_bucket" {
  description = "Provision the snapshot bucket in this module. Off if the operator wants to share an existing bucket."
  type        = bool
  default     = true
}

variable "ami_id" {
  description = "Override AMI. null = latest Amazon Linux 2023 ARM64 via SSM parameter."
  type        = string
  default     = null
}

variable "spot_max_price" {
  description = "Spot bid ceiling in USD/hr. null = pay the current spot price (auto-cap at on-demand). Setting a floor lets the box outbid brief spot spikes."
  type        = string
  default     = null
}
