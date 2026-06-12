variable "region" {
  description = "AWS region. Must match shared/temporal's region."
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

variable "incoming_prefix" {
  description = "S3 prefix the SQS notification listens on. PUT of {prefix}<id>/<file>.pdf fires the consumer."
  type        = string
  default     = "live/incoming/"

  validation {
    condition     = endswith(var.incoming_prefix, "/")
    error_message = "incoming_prefix must end with a slash."
  }
}

variable "pdf_suffix" {
  description = "S3 suffix filter for the notification. Restricts triggers to PDFs."
  type        = string
  default     = ".pdf"
}

variable "queue_visibility_timeout_s" {
  description = "SQS visibility timeout. Must exceed the longest plausible workflow-start latency from the consumer."
  type        = number
  default     = 300
}

variable "queue_message_retention_s" {
  description = "SQS message retention. 14 days is the AWS max — keeps undeliverable messages around for triage."
  type        = number
  default     = 1209600
}

# Remote-state coordinates — defaults match shared/terraform/_backend.hcl.
variable "state_bucket" {
  description = "S3 bucket holding terraform state. Defaults match _backend.hcl."
  type        = string
  default     = "ocr-benchmarking-tfstate"
}

variable "state_region" {
  description = "Region of the state bucket. Defaults match _backend.hcl."
  type        = string
  default     = "us-west-2"
}

variable "state_lock_table" {
  description = "DynamoDB lock table. Defaults match _backend.hcl."
  type        = string
  default     = "ocr-benchmarking-tflock"
}
