"""Durable payment intents and sandbox provider delivery receipts."""

from __future__ import annotations

import uuid

from django.db import models

from escrow.agreements.models import EscrowAgreement


class Transfer(models.Model):
    """One immutable financial intent; posting is owned by the ledger boundary."""

    class Kind(models.TextChoices):
        FUNDING = "FUNDING", "Funding"
        RELEASE = "RELEASE", "Release"
        REFUND = "REFUND", "Refund"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    class Provider(models.TextChoices):
        SANDBOX_PIX = "SANDBOX_PIX", "Sandbox PIX"
        INTERNAL = "INTERNAL", "Internal"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agreement = models.ForeignKey(
        EscrowAgreement,
        on_delete=models.PROTECT,
        related_name="transfers",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, choices=EscrowAgreement.Currency.choices)
    provider = models.CharField(max_length=32, choices=Provider.choices)
    provider_reference = models.CharField(max_length=128)
    provider_event_id = models.CharField(max_length=128, null=True, blank=True)
    idempotency_key = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_minor__gt=0),
                name="payments_transfer_amount_minor_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(currency__in=["BRL", "USD"]),
                name="payments_transfer_currency_is_brl_or_usd",
            ),
            models.UniqueConstraint(
                fields=["agreement", "kind"],
                name="payments_transfer_one_kind_per_agreement",
            ),
            models.UniqueConstraint(
                fields=["agreement", "idempotency_key"],
                name="payments_transfer_idempotency_per_agreement",
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_reference"],
                name="payments_transfer_provider_reference_unique",
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_event_id"],
                name="payments_transfer_provider_event_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["agreement", "status"], name="payments_tr_agreeme_68b233_idx"),
        ]


class SandboxPixCharge(models.Model):
    """The one simulated PIX charge issued for an agreement's hosted checkout."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CONFIRMED = "CONFIRMED", "Confirmed"
        REJECTED = "REJECTED", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agreement = models.OneToOneField(
        EscrowAgreement,
        on_delete=models.PROTECT,
        related_name="sandbox_pix_charge",
    )
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, choices=EscrowAgreement.Currency.choices)
    provider_reference = models.CharField(max_length=128, unique=True)
    idempotency_key = models.CharField(max_length=255)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_minor__gt=0),
                name="payments_charge_amount_minor_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(currency__in=["BRL", "USD"]),
                name="payments_charge_currency_is_brl_or_usd",
            ),
        ]


class ProviderCallbackReceipt(models.Model):
    """A compact, deduplicable receipt; raw provider bodies are never retained."""

    class Provider(models.TextChoices):
        SANDBOX_PIX = "SANDBOX_PIX", "Sandbox PIX"

    class Outcome(models.TextChoices):
        CONFIRMED = "CONFIRMED", "Confirmed"
        REJECTED = "REJECTED", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=32, choices=Provider.choices)
    provider_event_id = models.CharField(max_length=128)
    charge = models.ForeignKey(
        SandboxPixCharge,
        on_delete=models.PROTECT,
        related_name="callback_receipts",
    )
    transfer = models.ForeignKey(
        Transfer,
        on_delete=models.PROTECT,
        related_name="provider_callback_receipts",
        null=True,
        blank=True,
    )
    outcome = models.CharField(max_length=16, choices=Outcome.choices)
    payload_hash = models.CharField(max_length=64)
    signature_timestamp = models.PositiveBigIntegerField()
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["received_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_event_id"],
                name="payments_callback_provider_event_unique",
            ),
        ]
