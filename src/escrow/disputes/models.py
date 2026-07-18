"""Dispute, private-evidence, and access-capability records."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q

from escrow.agreements.models import EscrowAgreement


class Dispute(models.Model):
    """One customer challenge that freezes a single escrow agreement."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        REPORT_GENERATING = "REPORT_GENERATING", "Report generating"
        ANALYST_REVIEW = "ANALYST_REVIEW", "Analyst review"
        ADMIN_REVIEW = "ADMIN_REVIEW", "Admin review"
        RESOLVED = "RESOLVED", "Resolved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agreement = models.OneToOneField(
        EscrowAgreement,
        on_delete=models.PROTECT,
        related_name="dispute",
    )
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.OPEN)
    opened_at = models.DateTimeField()
    sla_due_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["opened_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    status__in=[
                        "OPEN",
                        "REPORT_GENERATING",
                        "ANALYST_REVIEW",
                        "ADMIN_REVIEW",
                        "RESOLVED",
                    ]
                ),
                name="disputes_status_is_known",
            ),
            models.CheckConstraint(
                condition=Q(sla_due_at__gt=F("opened_at")),
                name="disputes_sla_after_opening",
            ),
        ]
        indexes = [models.Index(fields=["status", "opened_at"])]


class Evidence(models.Model):
    """Metadata for a private object held outside PostgreSQL in Ceph RGW."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dispute = models.ForeignKey(
        Dispute,
        on_delete=models.PROTECT,
        related_name="evidence",
    )
    object_key = models.CharField(max_length=512, unique=True)
    extension = models.CharField(max_length=8)
    media_type = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    uploaded_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["uploaded_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(size_bytes__gt=0),
                name="disputes_evidence_size_positive",
            ),
        ]
        indexes = [models.Index(fields=["sha256"])]


class EvidenceAccessGrant(models.Model):
    """A hashed, short-lived capability for one internal evidence access."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    evidence = models.ForeignKey(
        Evidence,
        on_delete=models.PROTECT,
        related_name="access_grants",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="evidence_access_grants",
    )
    token_hash = models.CharField(max_length=64, unique=True)
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    last_accessed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["issued_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(expires_at__gt=F("issued_at")),
                name="disputes_evidence_grant_expires_after_issue",
            )
        ]
        indexes = [models.Index(fields=["evidence", "expires_at"])]
