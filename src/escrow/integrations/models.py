"""Organization API credentials; raw secrets are intentionally never stored."""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

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
