"""Small explicit writer for immutable audit records."""

from __future__ import annotations

from typing import Any

from escrow.agreements.models import EscrowAgreement
from escrow.audit.models import AuditEvent
from escrow.identity.models import User
from escrow.organizations.models import Organization


def record_audit_event(
    *,
    event_type: str,
    payload: dict[str, Any],
    correlation_id: str = "",
    organization: Organization | None = None,
    agreement: EscrowAgreement | None = None,
    actor: User | None = None,
) -> AuditEvent:
    """Persist one attributable fact; callers must pass an intentionally safe payload."""
    return AuditEvent.objects.create(
        event_type=event_type,
        payload=payload,
        correlation_id=correlation_id,
        organization=organization,
        agreement=agreement,
        actor=actor,
    )
