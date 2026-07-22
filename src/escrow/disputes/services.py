"""Transactional dispute operations independent from HTTP and object storage."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.audit.services import record_audit_event
from escrow.delivery.models import CustomerOtpChallenge
from escrow.delivery.services import authorize_customer_inspection_action
from escrow.disputes.evidence import prepare_evidence_upload
from escrow.disputes.models import Dispute, Evidence, EvidenceAccessGrant
from escrow.disputes.storage import presign_evidence_download, store_evidence_object
from escrow.identity.models import User
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.outbox import enqueue_outbox_event
from escrow.messaging.topology import RISK_DISPUTE_QUEUE
from escrow.notifications.outbox import enqueue_agreement_status_changed
from escrow.risk.services import PLATFORM_ADMIN_GROUP, RISK_DISPUTE_ANALYST_GROUP

DISPUTE_SLA = timedelta(hours=72)
_EVALUATE_DISPUTE_RISK_NAMESPACE = UUID("7c3f819a-9e12-4277-b9c1-45607593c200")



class DisputeAgreementNotFound(LookupError):
    """The requested agreement does not exist."""


class DisputeStateConflict(RuntimeError):
    """The agreement cannot be disputed in its current lifecycle state."""


class DisputeAlreadyOpen(RuntimeError):
    """An agreement can have only one customer dispute."""


class EvidenceNotFound(LookupError):
    """The requested evidence or access grant does not exist."""


class EvidenceAccessForbidden(PermissionError):
    """The caller lacks the platform-staff capability required for evidence access."""


class EvidenceAccessExpired(RuntimeError):
    """A time-limited evidence access grant is no longer valid."""


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


def issue_evidence_access_grant(
    *,
    dispute_id: UUID,
    evidence_id: UUID,
    actor: User,
    correlation_id: str,
    now: datetime | None = None,
) -> tuple[EvidenceAccessGrant, str]:
    """Issue one hashed, short-lived download capability to authorized staff."""
    issued_at = _timestamp(now)
    _require_evidence_reader(actor)
    with transaction.atomic():
        try:
            evidence = (
                Evidence.objects.select_related("dispute__agreement__organization")
                .get(id=evidence_id, dispute_id=dispute_id)
            )
        except Evidence.DoesNotExist as error:
            raise EvidenceNotFound from error
        access_token = _new_evidence_access_token()
        grant = EvidenceAccessGrant.objects.create(
            evidence=evidence,
            actor=actor,
            token_hash=_evidence_access_token_hash(access_token),
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=settings.EVIDENCE_ACCESS_GRANT_TTL_SECONDS),
        )
        record_audit_event(
            event_type="evidence_access_granted",
            organization=evidence.dispute.agreement.organization,
            agreement=evidence.dispute.agreement,
            actor=actor,
            correlation_id=correlation_id,
            payload={
                "dispute_id": str(dispute_id),
                "evidence_id": str(evidence.id),
                "grant_id": str(grant.id),
            },
        )
        return grant, access_token


def download_evidence_with_grant(
    *,
    access_token: str,
    s3_client: object,
    correlation_id: str,
    now: datetime | None = None,
) -> tuple[str, EvidenceAccessGrant]:
    """Audit one authorized access and return a short-lived pre-signed URL."""
    accessed_at = _timestamp(now)
    with transaction.atomic():
        try:
            grant = (
                EvidenceAccessGrant.objects.select_for_update()
                .select_related("evidence__dispute__agreement__organization", "actor")
                .get(token_hash=_evidence_access_token_hash(access_token))
            )
        except EvidenceAccessGrant.DoesNotExist as error:
            raise EvidenceNotFound from error
        if grant.expires_at <= accessed_at:
            raise EvidenceAccessExpired
        download_url = presign_evidence_download(
            s3_client,
            object_key=grant.evidence.object_key,
            ttl_seconds=settings.EVIDENCE_DOWNLOAD_URL_TTL_SECONDS,
        )
        grant.last_accessed_at = accessed_at
        grant.save(update_fields=["last_accessed_at"])
        record_audit_event(
            event_type="evidence_accessed",
            organization=grant.evidence.dispute.agreement.organization,
            agreement=grant.evidence.dispute.agreement,
            actor=grant.actor,
            correlation_id=correlation_id,
            payload={
                "evidence_id": str(grant.evidence_id),
                "grant_id": str(grant.id),
                "sha256": grant.evidence.sha256,
            },
        )
        return download_url, grant


def _require_evidence_reader(actor: User) -> None:
    """Allow only active risk-dispute analysts or platform admins, never organizations."""
    if not actor.is_authenticated or not actor.is_active or not actor.is_staff:
        raise EvidenceAccessForbidden
    if not actor.groups.filter(
        name__in=[RISK_DISPUTE_ANALYST_GROUP, PLATFORM_ADMIN_GROUP]
    ).exists():
        raise EvidenceAccessForbidden


def _new_evidence_access_token() -> str:
    return f"eva_{secrets.token_urlsafe(32)}"


def _evidence_access_token_hash(token: str) -> str:
    return hashlib.sha256(f"evidence-access:v1:{token}".encode()).hexdigest()


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
            status=Dispute.Status.REPORT_GENERATING,
        )
        agreement.status = EscrowAgreement.Status.DISPUTED
        agreement.version += 1
        agreement.realtime_sequence += 1
        agreement.save(update_fields=["status", "version", "realtime_sequence", "updated_at"])
        enqueue_outbox_event(
            MessageEnvelope.build(
                message_id=UUID(int=dispute.id.int ^ _EVALUATE_DISPUTE_RISK_NAMESPACE.int),
                message_type="EvaluateDisputeRisk.v1",
                version=1,
                occurred_at=opened_at,
                correlation_id=correlation_id,
                causation_id=str(dispute.id),
                tenant_id=str(agreement.organization_id),
                payload={
                    "agreement_id": str(agreement.id),
                    "dispute_id": str(dispute.id),
                },
            ),
            routing_key=RISK_DISPUTE_QUEUE.name,
        )
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
