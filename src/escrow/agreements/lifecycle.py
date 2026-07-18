"""Locked escrow-agreement transitions shared by HTTP and asynchronous workers."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from django.db import transaction

from escrow.agreements.models import EscrowAgreement


class AgreementStateConflict(RuntimeError):
    """A competing action already moved an agreement to another state."""


def start_funding(agreement_id: UUID) -> EscrowAgreement:
    """Move one checkout from waiting for PIX to payment processing."""
    return _transition(
        agreement_id,
        expected={EscrowAgreement.Status.AWAITING_PAYMENT},
        target=EscrowAgreement.Status.FUNDING_PROCESSING,
    )


def confirm_funding(agreement_id: UUID, *, confirmed_at: datetime) -> EscrowAgreement:
    """Record when the provider confirmed PIX and derive the delivery deadline."""
    if confirmed_at.tzinfo is None:
        raise ValueError("confirmed_at must be timezone-aware")
    with transaction.atomic():
        agreement = EscrowAgreement.objects.select_for_update().get(id=agreement_id)
        if (
            agreement.status != EscrowAgreement.Status.FUNDING_PROCESSING
            or agreement.funding_confirmed_at is not None
        ):
            raise AgreementStateConflict
        agreement.funding_confirmed_at = confirmed_at
        agreement.delivery_due_at = confirmed_at + timedelta(days=agreement.delivery_window_days)
        _increment_version_and_sequence(agreement)
        agreement.save(
            update_fields=[
                "funding_confirmed_at",
                "delivery_due_at",
                "version",
                "realtime_sequence",
                "updated_at",
            ]
        )
        return agreement


def mark_funds_held(agreement_id: UUID) -> EscrowAgreement:
    """Move approved, funded value into the escrow-held agreement state."""
    return _transition(
        agreement_id,
        expected={EscrowAgreement.Status.FUNDING_PROCESSING},
        target=EscrowAgreement.Status.HELD,
    )


def _transition(
    agreement_id: UUID,
    *,
    expected: set[str],
    target: str,
) -> EscrowAgreement:
    with transaction.atomic():
        agreement = EscrowAgreement.objects.select_for_update().get(id=agreement_id)
        if agreement.status not in expected:
            raise AgreementStateConflict
        agreement.status = target
        _increment_version_and_sequence(agreement)
        agreement.save(update_fields=["status", "version", "realtime_sequence", "updated_at"])
        return agreement


def _increment_version_and_sequence(agreement: EscrowAgreement) -> None:
    agreement.version += 1
    agreement.realtime_sequence += 1
