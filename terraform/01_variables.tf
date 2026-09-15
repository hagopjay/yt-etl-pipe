############################################
# 01_variables.tf
############################################

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "project_name" {
  type    = string
  default = "youtube-etl-pipeline"
}

variable "alert_email" {
  type        = string
  default     = ""
  description = "Ops alerts + human-review-request emails go here. Leave blank to skip subscription."
}

# --- Transcript queue behavior --------------------------------------------
variable "enable_asr_fallback" {
  type        = bool
  default     = true
  description = "If true, videos with no captions fall through to AWS Transcribe ASR instead of terminating as NO_CAPTIONS."
}

variable "respect_creator_disabled_captions" {
  type        = bool
  default     = true
  description = <<-EOT
    If true, a video where the CREATOR explicitly disabled captions (TranscriptsDisabled,
    distinct from "not yet generated") is treated as a terminal state and NEVER routed to
    ASR fallback, on the theory that disabling captions is a deliberate creator choice we
    should respect. Set false to override that and always attempt ASR regardless of the
    disabled-captions signal. This is a product/ethics decision, not just a technical one
    -- see docs/ARCHITECTURE_INSIGHTS.md section on this.
  EOT
}

variable "asr_confidence_review_threshold" {
  type        = number
  default     = 0.80
  description = "Average AWS Transcribe confidence below this routes to human review instead of auto-publishing."
}

variable "review_token_ttl_hours" {
  type        = number
  default     = 72
  description = "How long a human-review approve/reject link remains valid before DynamoDB TTL expires it."
}

variable "max_fetch_retry_attempts" {
  type        = number
  default     = 4
  description = "Step Functions retry ceiling for transient IP-block/rate-limit errors from the caption fetch step."
}
