"""Transactional dispute operations independent from HTTP and object storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.audit.services import record_audit_event
from escrow.delivery.models import CustomerOtpChallenge
from escrow.delivery.services import authorize_customer_inspection_action
from escrow.disputes.evidence import prepare_evidence_upload
from escrow.disputes.models import Dispute, Evidence
from escrow.disputes.storage import store_evidence_object
from escrow.notifications.outbox import enqueue_agreement_status_changed

DISPUTE_SLA = timedelta(hours=72)


class DisputeAgreementNotFound(LookupError):
    """The requested agreement does not exist."""


class DisputeStateConflict(RuntimeError):
    """The agreement cannot be disputed in its current lifecycle state."""


class DisputeAlreadyOpen(RuntimeError):
    """An agreement can have only one customer dispute."""


@dataclass(frozen=True, slots=True)
class OpenDisputeResult:
    dispute: Dispute


def open_customer_dispute(
    *,
    checkout_token: str,
    challenge_id: UUID,
    dispute_token: str,
    correlation_id: str,
    now: datetime | None = None,
) -> OpenDisputeResult:
    """Open the single customer dispute authorized by a fresh dispute OTP capability."""
    opened_at = _timestamp(now)
    with transaction.atomic():
        agreement, challenge = authorize_customer_inspection_action(
            checkout_token=checkout_token,
            challenge_id=challenge_id,
            action_token=dispute_token,
            purpose=CustomerOtpChallenge.Purpose.DISPUTE,
            now=opened_at,
        )
        result = open_dispute_after_customer_authorization(
            agreement_id=agreement.id,
            correlation_id=correlation_id,
            now=opened_at,
        )
        challenge.consumed_at = result.dispute.opened_at
        challenge.save(update_fields=["consumed_at"])
        return result


def attach_customer_evidence(
    *,
    checkout_token: str,
    dispute_id: UUID,
    challenge_id: UUID,
    dispute_token: str,
    filename: str,
    content: bytes,
    s3_client: object,
    correlation_id: str,
    now: datetime | None = None,
) -> Evidence:
    """Store one validated customer file privately and persist only its metadata.

    The object write happens inside the database transaction; a database
    failure can orphan an unreferenced object, which periodic reconciliation
    may remove. PostgreSQL never stores file contents.
    """
    uploaded_at = _timestamp(now)
    with transaction.atomic():
        agreement, _challenge = authorize_customer_inspection_action(
            checkout_token=checkout_token,
            challenge_id=challenge_id,
            action_token=dispute_token,
            purpose=CustomerOtpChallenge.Purpose.DISPUTE,
            now=uploaded_at,
        )
        try:
            dispute = Dispute.objects.select_for_update().get(
                id=dispute_id,
                agreement=agreement,
            )
        except Dispute.DoesNotExist as error:
            raise DisputeAgreementNotFound from error
        if agreement.status != EscrowAgreement.Status.DISPUTED:
            raise DisputeStateConflict("evidence requires a disputed agreement")
        prepared = prepare_evidence_upload(
            dispute_id=dispute.id,
            filename=filename,
            content=content,
        )
        store_evidence_object(
            s3_client,
            object_key=prepared.object_key,
            content=content,
            media_type=prepared.media_type,
        )
        evidence = Evidence.objects.create(
            id=prepared.evidence_id,
            dispute=dispute,
            object_key=prepared.object_key,
            extension=prepared.extension,
            media_type=prepared.media_type,
            size_bytes=prepared.size_bytes,
            sha256=prepared.sha256,
            uploaded_at=uploaded_at,
        )
        record_audit_event(
            event_type="evidence_uploaded",
            organization=agreement.organization,
            agreement=agreement,
            correlation_id=correlation_id,
            payload={"dispute_id": str(dispute.id), "evidence_id": str(evidence.id)},
        )
        return evidence


def open_dispute_after_customer_authorization(
    *,
    agreement_id: UUID,
    correlation_id: str,
    now: datetime | None = None,
) -> OpenDisputeResult:
    """Freeze a live inspection after the caller has verified customer OTP proof.

    OTP/check-out proof belongs to the customer transport boundary. This service
    owns the locked state transition and deliberately has no way to open a
    dispute outside the inspection window.
    """
    opened_at = _timestamp(now)
    with transaction.atomic():
        try:
            agreement = (
                EscrowAgreement.objects.select_for_update()
                .select_related("organization")
                .get(id=agreement_id)
            )
        except EscrowAgreement.DoesNotExist as error:
            raise DisputeAgreementNotFound from error
        if Dispute.objects.select_for_update().filter(agreement=agreement).exists():
            raise DisputeAlreadyOpen("agreement already has a dispute")
        if (
            agreement.status != EscrowAgreement.Status.INSPECTION
            or agreement.inspection_deadline_at is None
            or agreement.inspection_deadline_at <= opened_at
        ):
            raise DisputeStateConflict("only a live inspection can be disputed")

        dispute = Dispute.objects.create(
            agreement=agreement,
            opened_at=opened_at,
            sla_due_at=opened_at + DISPUTE_SLA,
        )
        agreement.status = EscrowAgreement.Status.DISPUTED
        agreement.version += 1
        agreement.realtime_sequence += 1
        agreement.save(update_fields=["status", "version", "realtime_sequence", "updated_at"])
        enqueue_agreement_status_changed(
            agreement,
            correlation_id=correlation_id,
            causation_id=str(dispute.id),
        )
        record_audit_event(
            event_type="dispute_opened",
            organization=agreement.organization,
            agreement=agreement,
            correlation_id=correlation_id,
            payload={"dispute_id": str(dispute.id)},
        )
        return OpenDisputeResult(dispute=dispute)


def _timestamp(now: datetime | None) -> datetime:
    if now is None:
        return timezone.now()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now
