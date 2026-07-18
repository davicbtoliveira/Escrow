"""Creation and lifecycle operations for opaque organization API credentials."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from escrow.integrations.models import ApiKey
from escrow.organizations.models import Organization

MAX_ACTIVE_API_KEYS = 2
VALID_API_KEY_SCOPES = frozenset(
    {
        "agreements:read",
        "agreements:write",
        "payments:read",
        "payments:write",
        "webhooks:manage",
    }
)


class ActiveApiKeyLimitError(RuntimeError):
    """An organization exhausted its deliberately small active-key budget."""


def fingerprint(raw_secret: str) -> str:
    """Make a deterministic HMAC fingerprint without persisting the secret."""
    return hmac.new(
        settings.API_KEY_HMAC_SECRET.encode(), raw_secret.encode(), hashlib.sha256
    ).hexdigest()


def _new_secret() -> tuple[str, str]:
    prefix = secrets.token_hex(4)
    return prefix, f"esk_{prefix}_{secrets.token_urlsafe(32)}"


def _active_keys(organization: Organization, now: datetime) -> QuerySet[ApiKey]:
    return ApiKey.objects.filter(organization=organization, revoked_at__isnull=True).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    )


def create_api_key(
    organization: Organization,
    *,
    name: str,
    scopes: list[str],
    expires_at: datetime | None = None,
) -> tuple[ApiKey, str]:
    """Issue a raw secret once, while serializing the active-key quota per tenant."""
    with transaction.atomic():
        locked_organization = Organization.objects.select_for_update().get(id=organization.id)
        now = timezone.now()
        if _active_keys(locked_organization, now).count() >= MAX_ACTIVE_API_KEYS:
            raise ActiveApiKeyLimitError
        for _ in range(3):
            prefix, raw_secret = _new_secret()
            try:
                with transaction.atomic():
                    api_key = ApiKey.objects.create(
                        organization=locked_organization,
                        name=name,
                        prefix=prefix,
                        secret_hash=fingerprint(raw_secret),
                        scopes=scopes,
                        expires_at=expires_at,
                    )
            except IntegrityError:
                continue
            return api_key, raw_secret
    raise RuntimeError("Could not allocate a unique API key prefix")


def rotate_api_key(
    api_key: ApiKey,
    *,
    overlap_seconds: int,
) -> tuple[ApiKey, str, ApiKey]:
    """Issue a replacement and retain the old credential only for the overlap."""
    with transaction.atomic():
        locked_key = (
            ApiKey.objects.select_for_update().select_related("organization").get(id=api_key.id)
        )
        now = timezone.now()
        if locked_key.status != "ACTIVE":
            raise ValueError("Only active API keys can be rotated")
        new_key, raw_secret = create_api_key(
            locked_key.organization,
            name=locked_key.name,
            scopes=locked_key.scopes,
            expires_at=None,
        )
        old_expiry = now + timedelta(seconds=overlap_seconds)
        if locked_key.expires_at is not None:
            old_expiry = min(old_expiry, locked_key.expires_at)
        locked_key.expires_at = old_expiry
        locked_key.save(update_fields=["expires_at", "updated_at"])
        return new_key, raw_secret, locked_key


def revoke_api_key(api_key: ApiKey) -> ApiKey:
    """Revoke idempotently so operator retries never restore a credential."""
    with transaction.atomic():
        locked_key = ApiKey.objects.select_for_update().get(id=api_key.id)
        if locked_key.revoked_at is None:
            locked_key.revoked_at = timezone.now()
            locked_key.save(update_fields=["revoked_at", "updated_at"])
        return locked_key
