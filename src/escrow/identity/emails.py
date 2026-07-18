"""Password-recovery delivery through Django test mail or MiniStack SES."""

from __future__ import annotations

from urllib.parse import urlencode

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.mail import send_mail

from escrow.identity.models import User


class EmailDeliveryError(RuntimeError):
    """The reset message could not be delivered to the configured provider."""


def send_password_recovery_email(user: User, uid: str, token: str) -> None:
    """Send the opaque password-reset capability only through the email channel."""
    query = urlencode({"uid": uid, "token": token})
    reset_url = f"{settings.FRONTEND_URL.rstrip('/')}/redefinir-senha/?{query}"
    subject = "Redefina sua senha do Escrow"
    body = (
        "Recebemos uma solicitação para redefinir sua senha no Escrow. "
        f"Use este link único: {reset_url}\n\n"
        "Se você não fez esta solicitação, ignore esta mensagem."
    )
    if settings.EMAIL_DELIVERY_BACKEND == "django":
        send_mail(subject, body, settings.SES_FROM_EMAIL, [user.email], fail_silently=False)
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
            Destination={"ToAddresses": [user.email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            },
        )
    except (BotoCoreError, ClientError) as error:
        raise EmailDeliveryError from error
