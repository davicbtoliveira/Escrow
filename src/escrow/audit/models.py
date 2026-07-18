"""Immutable attribution records for consequential platform actions."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from escrow.agreements.models import EscrowAgreement
from escrow.organizations.models import Organization


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="audit_events",
        null=True,
        blank=True,
    )
    agreement = models.ForeignKey(
        EscrowAgreement,
        on_delete=models.PROTECT,
        related_name="audit_events",
        null=True,
        blank=True,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="audit_events",
        null=True,
        blank=True,
    )
    event_type = models.CharField(max_length=100)
    correlation_id = models.CharField(max_length=128, blank=True)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(
                fields=["organization", "created_at"],
                name="audit_audi_organiz_7d7bda_idx",
            ),
            models.Index(
                fields=["agreement", "created_at"],
                name="audit_audi_agreeme_94978d_idx",
            ),
            models.Index(
                fields=["event_type", "created_at"],
                name="audit_audi_event_t_3ad60e_idx",
            ),
        ]
