"""OTP delivery through Django test mail or the Terraform-managed MiniStack SES."""

from __future__ import annotations

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.mail import send_mail


class CustomerOtpDeliveryError(RuntimeError):
    """The acceptance OTP could not reach the configured email provider."""


def send_customer_acceptance_otp(email: str, code: str) -> None:
    """Deliver an OTP without logging or persisting its plaintext value."""
    subject = "Confirme a entrega no Escrow"
    body = (
        f"Use o código {code} para confirmar a entrega no Escrow. "
        "Ele expira em 10 minutos. Se você não solicitou esta ação, ignore este email."
    )
    if settings.EMAIL_DELIVERY_BACKEND == "django":
        send_mail(subject, body, settings.SES_FROM_EMAIL, [email], fail_silently=False)
        return
    client = boto3.client(
        "ses",
        endpoint_url=settings.MINISTACK_ENDPOINT_URL,
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )
    try:
        client.send_email(
            Source=settings.SES_FROM_EMAIL,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            },
        )
    except (BotoCoreError, ClientError) as error:
        raise CustomerOtpDeliveryError from error
