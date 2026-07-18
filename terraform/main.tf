provider "aws" {
  region     = var.aws_region
  access_key = var.aws_access_key
  secret_key = var.aws_secret_key

  # MiniStack does not implement account or credential validation like AWS.
  skip_credentials_validation = var.ministack_endpoint != null
  skip_metadata_api_check     = var.ministack_endpoint != null
  skip_requesting_account_id  = var.ministack_endpoint != null

  dynamic "endpoints" {
    for_each = var.ministack_endpoint == null ? [] : [var.ministack_endpoint]

    content {
      ses            = endpoints.value
      secretsmanager = endpoints.value
      kms            = endpoints.value
    }
  }
}

resource "aws_ses_email_identity" "password_recovery" {
  email = var.ses_identity_email
}

resource "aws_ses_template" "password_recovery" {
  name    = var.password_recovery_template_name
  subject = "Recupere o acesso à sua conta Escrow"
  html    = <<-HTML
    <html>
      <body>
        <p>Olá,</p>
        <p>Use o código <strong>{{recovery_code}}</strong> para recuperar o acesso à sua conta.</p>
        <p>Se você não solicitou esta recuperação, ignore este email.</p>
      </body>
    </html>
  HTML
  text    = <<-TEXT
    Olá,

    Use o código {{recovery_code}} para recuperar o acesso à sua conta.

    Se você não solicitou esta recuperação, ignore este email.
  TEXT
}

resource "aws_kms_key" "application" {
  description             = "Local envelope-encryption key for Escrow simulated PII."
  deletion_window_in_days = 7
  enable_key_rotation     = true
  tags                    = var.tags
}

resource "aws_kms_alias" "application" {
  name          = "alias/escrow-local-application"
  target_key_id = aws_kms_key.application.key_id
}

resource "aws_secretsmanager_secret" "application" {
  name                    = var.application_secret_name
  description             = "Local application and webhook secrets for Escrow."
  kms_key_id              = aws_kms_key.application.arn
  recovery_window_in_days = 0
  tags                    = var.tags
}

resource "aws_secretsmanager_secret_version" "application" {
  secret_id     = aws_secretsmanager_secret.application.id
  secret_string = jsonencode(var.application_secret_values)
}
