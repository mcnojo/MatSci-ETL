variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-west-2"
}

variable "tags" {
  description = "Extra tags. Project/Env are added automatically."
  type        = map(string)
  default     = {}
}

variable "tree_llm_ssm_prefix" {
  description = "SSM parameter prefix for tree_llm API keys. Must match shared/temporal var of the same name."
  type        = string
  default     = "/ocr-bench/tree_llm"
}

variable "qdrant_ssm_prefix" {
  description = "SSM parameter prefix for Qdrant Cloud endpoint + API key. Must match downstream consumer vars of the same name."
  type        = string
  default     = "/ocr-bench/qdrant"
}
