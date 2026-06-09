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
  description = "Which OCR model this box serves. Becomes part of the role tag (e.g. role=vllm-chandra-prod)."
  type        = string
  default     = "chandra"
}

variable "vllm_port" {
  description = "Port vLLM listens on. Tagged onto the resolver URL (vllm-instance://<model>:<port>/...)."
  type        = number
  default     = 8004
}

variable "hf_model_id" {
  description = "Hugging Face model ID for `vllm serve`."
  type        = string
  default     = "datalab-to/chandra-ocr-2"
}

variable "vllm_extra_args" {
  description = "Extra args appended to `vllm serve`. Per-model tuning lives here."
  type        = string
  default     = ""
}

variable "instance_type" {
  description = "GPU instance type. g6.xlarge = 1× L4 24GB; bump to g6e.xlarge for 48GB if co-hosting two models."
  type        = string
  default     = "g6.xlarge"
}

variable "root_volume_gb" {
  description = "Root EBS. Sized for model weights (~20GB) + workspace + headroom."
  type        = number
  default     = 100
}

variable "vpc_id" {
  description = "VPC ID. null = default VPC. For prod, set to common/temporal's vpc_id output so workers can reach the box privately."
  type        = string
  default     = null
}

variable "subnet_id" {
  description = "Subnet. null = first subnet of the selected VPC."
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
