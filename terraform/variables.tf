variable "aws_region" {
  type        = string
  description = "AWS-compatible region used by MiniStack or a future AWS environment."
  default     = "us-east-1"
}

variable "ministack_endpoint" {
  type        = string
  description = "AWS-compatible MiniStack endpoint. Set to null to use native AWS service endpoints."
  default     = "http://localhost:4566"
  nullable    = true
}

variable "aws_access_key" {
  type        = string
  description = "AWS-compatible access key. Leave null to use the AWS provider credential chain."
  default     = null
  nullable    = true
  sensitive   = true
}

variable "aws_secret_key" {
  type        = string
  description = "AWS-compatible secret key. Leave null to use the AWS provider credential chain."
  default     = null
  nullable    = true
  sensitive   = true
}

variable "ses_identity_email" {
  type        = string
  description = "Sender identity used by the password-recovery email template."
  default     = "no-reply@escrow.local"

  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.ses_identity_email))
    error_message = "ses_identity_email must be a valid email address."
  }
}

variable "password_recovery_template_name" {
  type        = string
  description = "Stable SES template name consumed by the identity module."
  default     = "escrow-password-recovery"
}

variable "application_secret_name" {
  type        = string
  description = "Secrets Manager name for non-committed application and webhook secrets."
  default     = "/escrow/local/application"
}

variable "application_kms_alias" {
  type        = string
  description = "KMS alias consumed by the application for simulated PII envelope encryption."
  default     = "alias/escrow-local-application"

  validation {
    condition     = can(regex("^alias/[A-Za-z0-9/_-]+$", var.application_kms_alias))
    error_message = "application_kms_alias must be a valid KMS alias starting with alias/."
  }
}

variable "application_secret_values" {
  type        = map(string)
  description = "Application secrets persisted only in ignored local Terraform state."
  sensitive   = true
}

variable "tags" {
  type        = map(string)
  description = "Tags attached to AWS-compatible resources."
  default = {
    ManagedBy   = "terraform"
    Environment = "local"
    Project     = "escrow"
  }
}
