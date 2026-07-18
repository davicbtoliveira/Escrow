"""Persisted escrow terms and durable API idempotency responses."""

from __future__ import annotations

import uuid

from django.db import models

from escrow.organizations.models import Organization


class EscrowAgreement(models.Model):
    class Currency(models.TextChoices):
        BRL = "BRL", "Brazilian real"
        USD = "USD", "United States dollar"

    class Status(models.TextChoices):
        AWAITING_PAYMENT = "AWAITING_PAYMENT", "Awaiting payment"
        FUNDING_PROCESSING = "FUNDING_PROCESSING", "Funding processing"
        HELD = "HELD", "Held in escrow"
        REVIEW_REQUIRED = "REVIEW_REQUIRED", "Risk review required"
        FUNDING_REJECTED = "FUNDING_REJECTED", "Funding rejected"
        INSPECTION = "INSPECTION", "Inspection"
        DISPUTED = "DISPUTED", "Disputed"
        RELEASE_PENDING = "RELEASE_PENDING", "Release pending"
        RELEASED = "RELEASED", "Released"
        REFUND_PENDING = "REFUND_PENDING", "Refund pending"
        REFUNDED = "REFUNDED", "Refunded"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="agreements",
    )
    external_customer_id = models.CharField(max_length=128)
    customer_name_masked = models.CharField(max_length=200)
    customer_email_masked = models.CharField(max_length=254)
    customer_document_masked = models.CharField(max_length=32)
    customer_document_kind = models.CharField(max_length=4)
    customer_email_blind_index = models.CharField(max_length=64, db_index=True)
    customer_document_blind_index = models.CharField(max_length=64, db_index=True)
    customer_pii_ciphertext = models.BinaryField()
    customer_pii_nonce = models.BinaryField()
    customer_pii_encrypted_data_key = models.BinaryField()
    customer_pii_kms_key_id = models.CharField(max_length=512)
    checkout_token_hash = models.CharField(max_length=64, unique=True)
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, choices=Currency.choices)
    fee_bps = models.PositiveIntegerField()
    delivery_window_days = models.PositiveSmallIntegerField()
    funding_confirmed_at = models.DateTimeField(null=True, blank=True)
    delivery_due_at = models.DateTimeField(null=True, blank=True)
    inspection_deadline_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.AWAITING_PAYMENT,
    )
    version = models.PositiveIntegerField(default=0)
    realtime_sequence = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_minor__gt=0),
                name="agreements_amount_minor_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(currency__in=["BRL", "USD"]),
                name="agreements_currency_is_brl_or_usd",
            ),
            models.CheckConstraint(
                condition=models.Q(delivery_window_days__gte=1)
                & models.Q(delivery_window_days__lte=90),
                name="agreements_delivery_window_between_1_and_90",
            ),
            models.CheckConstraint(
                condition=models.Q(fee_bps__gte=0) & models.Q(fee_bps__lte=10_000),
                name="agreements_fee_bps_between_0_and_10000",
            ),
        ]


class IdempotencyRecord(models.Model):
    """The first successful mutation response, scoped to one organization route."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="idempotency_records",
    )
    agreement = models.ForeignKey(
        EscrowAgreement,
        on_delete=models.PROTECT,
        related_name="idempotency_records",
    )
    method = models.CharField(max_length=8)
    route = models.CharField(max_length=255)
    idempotency_key = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField()
    response_body = models.JSONField()
    checkout_token_ciphertext = models.BinaryField(null=True, blank=True)
    checkout_token_nonce = models.BinaryField(null=True, blank=True)
    checkout_token_encrypted_data_key = models.BinaryField(null=True, blank=True)
    checkout_token_kms_key_id = models.CharField(max_length=512, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "method", "route", "idempotency_key"],
                name="agreements_idempotency_scope_unique",
            )
        ]
        indexes = [models.Index(fields=["organization", "created_at"])]
