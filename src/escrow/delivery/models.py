"""Durable delivery reports and customer acceptance authorization records."""

from __future__ import annotations

import uuid

from django.db import models

from escrow.agreements.models import EscrowAgreement


class DeliveryReport(models.Model):
    """The single organization-declared delivery event for an agreement."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agreement = models.OneToOneField(
        EscrowAgreement,
        on_delete=models.PROTECT,
        related_name="delivery_report",
    )
    idempotency_key = models.CharField(max_length=255)
    reported_at = models.DateTimeField()
    inspection_deadline_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["reported_at", "id"]


class CustomerOtpChallenge(models.Model):
    """A short-lived, hashed capability for one customer inspection action."""

    class Purpose(models.TextChoices):
        DELIVERY_ACCEPTANCE = "DELIVERY_ACCEPTANCE", "Delivery acceptance"
        DISPUTE = "DISPUTE", "Dispute"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agreement = models.ForeignKey(
        EscrowAgreement,
        on_delete=models.PROTECT,
        related_name="customer_otp_challenges",
    )
    purpose = models.CharField(
        max_length=32,
        choices=Purpose.choices,
        default=Purpose.DELIVERY_ACCEPTANCE,
    )
    code_hash = models.CharField(max_length=64)
    sent_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    verification_attempts = models.PositiveSmallIntegerField(default=0)
    verified_at = models.DateTimeField(null=True, blank=True)
    authorization_token_hash = models.CharField(max_length=64, null=True, blank=True)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "id"]
        indexes = [models.Index(fields=["agreement", "created_at"])]
