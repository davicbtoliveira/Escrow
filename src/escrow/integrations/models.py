"""Organization API credentials; raw secrets are intentionally never stored."""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.organizations.models import Organization


class ApiKey(models.Model):
    """A scoped credential which is usable only while active and unexpired."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="api_keys",
    )
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=16, unique=True)
    secret_hash = models.CharField(max_length=64)
    scopes = models.JSONField(default=list)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_used_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "id"]

    @property
    def status(self) -> str:
        if self.revoked_at is not None:
            return "REVOKED"
        if self.expires_at is not None and self.expires_at <= timezone.now():
            return "EXPIRED"
        return "ACTIVE"


class WebhookEndpoint(models.Model):
    """One organization-controlled HTTPS destination and encrypted signing material."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="webhook_endpoints",
    )
    url = models.URLField(max_length=2048)
    secret_ciphertext = models.BinaryField()
    secret_nonce = models.BinaryField()
    secret_encrypted_data_key = models.BinaryField()
    secret_kms_key_id = models.CharField(max_length=512)
    previous_secret_ciphertext = models.BinaryField(null=True, blank=True)
    previous_secret_nonce = models.BinaryField(null=True, blank=True)
    previous_secret_encrypted_data_key = models.BinaryField(null=True, blank=True)
    previous_secret_kms_key_id = models.CharField(max_length=512, null=True, blank=True)
    previous_secret_expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "url"],
                name="integrations_webhook_endpoint_organization_url_unique",
            )
        ]


class WebhookEvent(models.Model):
    """A safe, immutable, versioned organization event ready for fan-out."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="webhook_events",
    )
    agreement = models.ForeignKey(
        EscrowAgreement,
        on_delete=models.PROTECT,
        related_name="webhook_events",
    )
    event_type = models.CharField(max_length=128)
    version = models.PositiveSmallIntegerField(default=1)
    sequence = models.PositiveBigIntegerField()
    correlation_id = models.CharField(max_length=128)
    causation_id = models.CharField(max_length=128, null=True, blank=True)
    payload = models.JSONField()
    occurred_at = models.DateTimeField()
    delivery_command_key = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["occurred_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["agreement", "event_type", "sequence"],
                name="integrations_webhook_event_agreement_sequence_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1),
                name="integrations_webhook_event_version_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "occurred_at"],
                name="integ_webhook_evt_org_idx",
            )
        ]


class WebhookDelivery(models.Model):
    """An independently retriable endpoint delivery for one immutable event."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RETRYING = "RETRYING", "Retrying"
        DELIVERED = "DELIVERED", "Delivered"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    endpoint = models.ForeignKey(
        WebhookEndpoint,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )
    event = models.ForeignKey(
        WebhookEvent,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    replay_count = models.PositiveIntegerField(default=0)
    first_attempt_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["endpoint", "event"],
                name="integrations_webhook_delivery_endpoint_event_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=["status", "next_attempt_at"],
                name="integ_webhook_del_due_idx",
            )
        ]
