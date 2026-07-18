output "ses_identity_email" {
  description = "SES sender identity configured for password-recovery emails."
  value       = aws_ses_email_identity.password_recovery.email
}

output "password_recovery_template_name" {
  description = "SES template name the identity module must use."
  value       = aws_ses_template.password_recovery.name
}

output "application_secret_arn" {
  description = "Secrets Manager ARN for the application secret payload."
  value       = aws_secretsmanager_secret.application.arn
}

output "application_secret_name" {
  description = "Secrets Manager name for local application secret lookup."
  value       = aws_secretsmanager_secret.application.name
}

output "application_kms_key_arn" {
  description = "KMS key ARN used for local simulated envelope encryption."
  value       = aws_kms_key.application.arn
}

output "application_kms_key_alias" {
  description = "KMS alias the application uses to encrypt simulated PII."
  value       = aws_kms_alias.application.name
}
