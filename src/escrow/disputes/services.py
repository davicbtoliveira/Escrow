"""Transactional dispute operations independent from HTTP and object storage."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.money import calculate_release_fee_minor
from escrow.agreements.pii import CustomerIdentity, EncryptedValue, envelope_cipher
from escrow.agreements.services import customer_pii_context
from escrow.audit.services import record_audit_event
from escrow.delivery.models import CustomerOtpChallenge
from escrow.delivery.services import authorize_customer_inspection_action
from escrow.disputes.evidence import prepare_evidence_upload
from escrow.disputes.models import Dispute, DisputeAdminDecision, DisputeRecommendation, Evidence, EvidenceAccessGrant
from escrow.disputes.storage import presign_evidence_download, store_evidence_object
from escrow.identity.models import User
from escrow.ledger.models import LedgerTransaction
from escrow.ledger.services import LedgerEntryInput, LedgerPosting, post_ledger_transaction
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.outbox import enqueue_outbox_event
from escrow.messaging.topology import RISK_DISPUTE_QUEUE
from escrow.notifications.outbox import enqueue_agreement_status_changed
from escrow.payments.models import Transfer
from escrow.risk.models import DisputeRiskReport
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


class DisputeRecommendationForbidden(PermissionError):
    """The caller lacks the risk analyst role required for recommendations."""


class DisputeRecommendationConflict(RuntimeError):
    """The dispute has already been recommended or is not in ANALYST_REVIEW."""


class DisputeRecommendationValidationError(ValueError):
    """The recommendation payload or command ID is invalid."""


class DisputeAdminForbidden(PermissionError):
    """The caller lacks the platform admin role required for administrative dispute actions."""


class DisputeAdminSeparationError(RuntimeError):
    """The analyst who submitted the recommendation cannot approve it as admin."""




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


def get_dispute_analyst_dashboard(
    *,
    analyst: User,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return counts and masked dispute queues for authorized analysts only."""
    _require_evidence_reader(analyst)
    current_time = _timestamp(now)

    active_disputes = Dispute.objects.select_related(
        "agreement__organization"
    ).prefetch_related("risk_report", "analyst_recommendation").all()

    counts = {
        "OPEN": 0,
        "REPORT_GENERATING": 0,
        "ANALYST_REVIEW": 0,
        "ADMIN_REVIEW": 0,
        "on_track": 0,
        "at_risk": 0,
        "overdue": 0,
    }

    queue_items: list[dict[str, object]] = []

    for dispute in active_disputes:
        if dispute.status in counts:
            counts[dispute.status] += 1

        if dispute.status != Dispute.Status.RESOLVED:
            elapsed = current_time - dispute.opened_at
            if elapsed < timedelta(hours=48):
                sla_status = "ON_TRACK"
                counts["on_track"] += 1
            elif elapsed <= timedelta(hours=72):
                sla_status = "AT_RISK"
                counts["at_risk"] += 1
            else:
                sla_status = "OVERDUE"
                counts["overdue"] += 1

            agreement = dispute.agreement
            org = agreement.organization

            report_data = None
            if hasattr(dispute, "risk_report") and dispute.risk_report:
                rep: DisputeRiskReport = dispute.risk_report
                report_data = {
                    "id": str(rep.id),
                    "summary": rep.summary,
                    "suspicion_result": rep.suspicion_result,
                    "score": rep.score,
                    "flags": rep.flags,
                    "policy_version": rep.policy_version,
                    "timeline": rep.timeline,
                    "customer_history": rep.customer_history,
                    "organization_history": rep.organization_history,
                    "evidence_integrity": rep.evidence_integrity,
                    "inputs": rep.inputs,
                    "generated_at": rep.generated_at.isoformat().replace("+00:00", "Z"),
                }

            rec_data = None
            if hasattr(dispute, "analyst_recommendation") and dispute.analyst_recommendation:
                rec: DisputeRecommendation = dispute.analyst_recommendation
                rec_data = {
                    "id": str(rec.id),
                    "recommendation": rec.recommendation,
                    "rationale": rec.rationale,
                    "recommended_at": rec.recommended_at.isoformat().replace("+00:00", "Z"),
                }

            queue_items.append(
                {
                    "dispute_id": str(dispute.id),
                    "agreement_id": str(agreement.id),
                    "status": dispute.status,
                    "sla_status": sla_status,
                    "opened_at": dispute.opened_at.isoformat().replace("+00:00", "Z"),
                    "sla_due_at": dispute.sla_due_at.isoformat().replace("+00:00", "Z"),
                    "organization": {
                        "id": str(org.id),
                        "name_masked": f"{org.name[:1]}***" if org.name else "***",
                    },
                    "customer": {
                        "name": agreement.customer_name_masked,
                        "email_masked": agreement.customer_email_masked,
                        "document_masked": agreement.customer_document_masked,
                    },
                    "amount_minor": agreement.amount_minor,
                    "currency": agreement.currency,
                    "report": report_data,
                    "recommendation": rec_data,
                }
            )

    return {"counts": counts, "queue": queue_items}


def submit_dispute_recommendation(
    *,
    dispute_id: UUID | str,
    analyst: User,
    recommendation: str,
    command_id: str,
    rationale: str,
    correlation_id: str = "",
    now: datetime | None = None,
) -> tuple[DisputeRecommendation, bool]:
    """Commit one analyst recommendation and transition dispute to ADMIN_REVIEW."""
    _require_evidence_reader(analyst)
    if recommendation not in DisputeRecommendation.Choice.values:
        raise DisputeRecommendationValidationError("recommendation choice is invalid")
    if not isinstance(command_id, str) or not command_id.strip() or len(command_id.strip()) > 128:
        raise DisputeRecommendationValidationError("command_id is invalid")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale.strip()) > 1_000:
        raise DisputeRecommendationValidationError("rationale is invalid")

    norm_command_id = command_id.strip()
    norm_rationale = rationale.strip()
    recommended_at = _timestamp(now)
    try:
        dispute_uuid = dispute_id if isinstance(dispute_id, UUID) else UUID(str(dispute_id))
    except (TypeError, ValueError) as error:
        raise DisputeRecommendationValidationError("dispute_id is invalid") from error

    eff_correlation_id = (
        correlation_id.strip()
        if correlation_id and correlation_id.strip()
        else f"dispute-rec-{dispute_uuid}"
    )

    with transaction.atomic():
        try:
            dispute = (
                Dispute.objects.select_for_update()
                .select_related("agreement__organization")
                .get(id=dispute_uuid)
            )
        except Dispute.DoesNotExist as error:
            raise DisputeAgreementNotFound from error

        existing = (
            DisputeRecommendation.objects.select_for_update()
            .filter(dispute=dispute)
            .first()
        )
        if existing is not None:
            if existing.command_id == norm_command_id:
                if (
                    existing.recommendation == recommendation
                    and existing.rationale == norm_rationale
                ):
                    return existing, True
                raise DisputeRecommendationValidationError(
                    "command intent differs from first execution"
                )
            raise DisputeRecommendationConflict("dispute has already been recommended")

        if dispute.status != Dispute.Status.ANALYST_REVIEW:
            raise DisputeStateConflict("dispute recommendation requires ANALYST_REVIEW status")

        try:
            report = dispute.risk_report
        except Exception:
            report = DisputeRiskReport.objects.filter(dispute=dispute).first()
            if report is None:
                raise DisputeStateConflict(
                    "dispute risk report must exist before recommendation"
                )

        rec = DisputeRecommendation.objects.create(
            dispute=dispute,
            report=report,
            analyst=analyst,
            command_id=norm_command_id,
            recommendation=recommendation,
            rationale=norm_rationale,
            recommended_at=recommended_at,
        )

        dispute.status = Dispute.Status.ADMIN_REVIEW
        dispute.save(update_fields=["status", "updated_at"])

        enqueue_agreement_status_changed(
            dispute.agreement,
            correlation_id=eff_correlation_id,
            causation_id=str(rec.id),
        )

        record_audit_event(
            event_type="dispute_recommendation_submitted",
            organization=dispute.agreement.organization,
            agreement=dispute.agreement,
            actor=analyst,
            correlation_id=eff_correlation_id,
            payload={
                "dispute_id": str(dispute.id),
                "recommendation_id": str(rec.id),
                "recommendation": recommendation,
                "command_id": norm_command_id,
            },
        )
        return rec, False


def require_platform_admin(admin: User) -> None:
    """Enforce an explicit platform-admin role with no analyst-only access."""
    if not admin.is_active or not admin.is_staff:
        raise DisputeAdminForbidden("platform admin capability is required")
    if not admin.groups.filter(name=PLATFORM_ADMIN_GROUP).exists():
        raise DisputeAdminForbidden("platform admin capability is required")


def decrypt_dispute_customer_pii(
    *,
    dispute_id: UUID | str,
    admin: User,
    reason: str,
    correlation_id: str = "",
) -> dict[str, str]:
    """Decrypt customer PII for platform admin investigation after recording audit event."""
    require_platform_admin(admin)
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1_000:
        raise DisputeRecommendationValidationError("reason is invalid")

    norm_reason = reason.strip()
    try:
        dispute_uuid = dispute_id if isinstance(dispute_id, UUID) else UUID(str(dispute_id))
    except (TypeError, ValueError) as error:
        raise DisputeRecommendationValidationError("dispute_id is invalid") from error

    eff_correlation_id = (
        correlation_id.strip()
        if correlation_id and correlation_id.strip()
        else f"dispute-pii-{dispute_uuid}"
    )

    with transaction.atomic():
        try:
            dispute = (
                Dispute.objects.select_for_update()
                .select_related("agreement__organization")
                .get(id=dispute_uuid)
            )
        except Dispute.DoesNotExist as error:
            raise DisputeAgreementNotFound from error

        agreement = dispute.agreement
        cipher = envelope_cipher()
        encrypted = EncryptedValue(
            ciphertext=agreement.customer_pii_ciphertext,
            nonce=agreement.customer_pii_nonce,
            encrypted_data_key=agreement.customer_pii_encrypted_data_key,
            kms_key_id=agreement.customer_pii_kms_key_id,
        )
        context = customer_pii_context(agreement.organization_id, agreement.id)
        plaintext_bytes = cipher.decrypt(encrypted, context)
        plaintext_dict = json.loads(plaintext_bytes.decode())
        customer = CustomerIdentity(
            name=plaintext_dict["name"],
            email=plaintext_dict["email"],
            document=plaintext_dict["document"],
            document_kind=agreement.customer_document_kind,
        )

        record_audit_event(
            event_type="dispute_customer_pii_decrypted",
            organization=agreement.organization,
            agreement=agreement,
            actor=admin,
            correlation_id=eff_correlation_id,
            payload={
                "dispute_id": str(dispute.id),
                "reason": norm_reason,
            },
        )

        return {
            "name": customer.name,
            "email": customer.email,
            "document": customer.document,
            "document_kind": customer.document_kind,
        }


def get_dispute_admin_dashboard(
    *,
    admin: User,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return administrative dispute queue and SLA metrics for PLATFORM_ADMIN."""
    require_platform_admin(admin)
    current_time = _timestamp(now)

    all_disputes = Dispute.objects.select_related(
        "agreement__organization"
    ).prefetch_related("risk_report", "analyst_recommendation", "admin_decision").all()

    counts = {
        "open": 0,
        "closed": 0,
        "at_risk": 0,
        "overdue": 0,
        "awaiting_admin_decision": 0,
    }

    queue_items: list[dict[str, object]] = []

    for dispute in all_disputes:
        is_closed = (dispute.status == Dispute.Status.RESOLVED)
        if is_closed:
            counts["closed"] += 1
        else:
            counts["open"] += 1
            if dispute.status == Dispute.Status.ADMIN_REVIEW:
                counts["awaiting_admin_decision"] += 1

            sla_reference_time = (
                dispute.admin_decision.decided_at
                if hasattr(dispute, "admin_decision") and dispute.admin_decision
                else current_time
            )
            elapsed = sla_reference_time - dispute.opened_at

            if elapsed < timedelta(hours=48):
                sla_status = "ON_TRACK"
            elif elapsed <= timedelta(hours=72):
                sla_status = "AT_RISK"
                counts["at_risk"] += 1
            else:
                sla_status = "OVERDUE"
                counts["overdue"] += 1

        if dispute.status == Dispute.Status.ADMIN_REVIEW:
            agreement = dispute.agreement
            org = agreement.organization

            report_data = None
            if hasattr(dispute, "risk_report") and dispute.risk_report:
                rep: DisputeRiskReport = dispute.risk_report
                report_data = {
                    "id": str(rep.id),
                    "summary": rep.summary,
                    "suspicion_result": rep.suspicion_result,
                    "score": rep.score,
                    "flags": rep.flags,
                    "policy_version": rep.policy_version,
                    "timeline": rep.timeline,
                    "customer_history": rep.customer_history,
                    "organization_history": rep.organization_history,
                    "evidence_integrity": rep.evidence_integrity,
                    "inputs": rep.inputs,
                    "generated_at": rep.generated_at.isoformat().replace("+00:00", "Z"),
                }

            rec_data = None
            if hasattr(dispute, "analyst_recommendation") and dispute.analyst_recommendation:
                rec: DisputeRecommendation = dispute.analyst_recommendation
                rec_data = {
                    "id": str(rec.id),
                    "analyst_id": str(rec.analyst_id),
                    "recommendation": rec.recommendation,
                    "rationale": rec.rationale,
                    "recommended_at": rec.recommended_at.isoformat().replace("+00:00", "Z"),
                }

            queue_items.append(
                {
                    "dispute_id": str(dispute.id),
                    "agreement_id": str(agreement.id),
                    "status": dispute.status,
                    "sla_status": sla_status,
                    "opened_at": dispute.opened_at.isoformat().replace("+00:00", "Z"),
                    "sla_due_at": dispute.sla_due_at.isoformat().replace("+00:00", "Z"),
                    "organization": {
                        "id": str(org.id),
                        "name_masked": f"{org.name[:1]}***" if org.name else "***",
                    },
                    "customer": {
                        "name": agreement.customer_name_masked,
                        "email_masked": agreement.customer_email_masked,
                        "document_masked": agreement.customer_document_masked,
                    },
                    "amount_minor": agreement.amount_minor,
                    "currency": agreement.currency,
                    "report": report_data,
                    "recommendation": rec_data,
                }
            )

    return {"counts": counts, "queue": queue_items}


def resolve_dispute_by_admin(
    *,
    dispute_id: UUID | str,
    admin: User,
    decision: str,
    command_id: str,
    rationale: str,
    correlation_id: str = "",
    now: datetime | None = None,
) -> tuple[DisputeAdminDecision, bool]:
    """Execute the final PLATFORM_ADMIN decision, posting ledger entry and resolving dispute."""
    require_platform_admin(admin)
    if decision not in DisputeAdminDecision.Choice.values:
        raise DisputeRecommendationValidationError("decision choice is invalid")
    if not isinstance(command_id, str) or not command_id.strip() or len(command_id.strip()) > 128:
        raise DisputeRecommendationValidationError("command_id is invalid")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale.strip()) > 1_000:
        raise DisputeRecommendationValidationError("rationale is invalid")

    norm_command_id = command_id.strip()
    norm_rationale = rationale.strip()
    decided_at = _timestamp(now)
    try:
        dispute_uuid = dispute_id if isinstance(dispute_id, UUID) else UUID(str(dispute_id))
    except (TypeError, ValueError) as error:
        raise DisputeRecommendationValidationError("dispute_id is invalid") from error

    eff_correlation_id = (
        correlation_id.strip()
        if correlation_id and correlation_id.strip()
        else f"dispute-admin-{dispute_uuid}"
    )

    with transaction.atomic():
        try:
            dispute = (
                Dispute.objects.select_for_update()
                .select_related("agreement__organization", "analyst_recommendation")
                .get(id=dispute_uuid)
            )
        except Dispute.DoesNotExist as error:
            raise DisputeAgreementNotFound from error

        existing = (
            DisputeAdminDecision.objects.select_for_update()
            .filter(dispute=dispute)
            .first()
        )
        if existing is not None:
            if existing.command_id == norm_command_id:
                if existing.decision == decision and existing.rationale == norm_rationale:
                    return existing, True
                raise DisputeRecommendationValidationError(
                    "command intent differs from first execution"
                )
            raise DisputeRecommendationConflict("dispute has already been resolved by admin")

        if dispute.status != Dispute.Status.ADMIN_REVIEW:
            raise DisputeStateConflict("dispute resolution requires ADMIN_REVIEW status")

        try:
            rec = dispute.analyst_recommendation
        except Exception:
            rec = DisputeRecommendation.objects.filter(dispute=dispute).first()
            if rec is None:
                raise DisputeStateConflict("analyst recommendation must exist before admin resolution")

        if rec.analyst_id == admin.id:
            raise DisputeAdminSeparationError(
                "separation of duties: analyst who recommended cannot be the admin who approves"
            )

        agreement = dispute.agreement

        if decision == DisputeAdminDecision.Choice.RELEASE_TO_ORGANIZATION:
            release_transfer, _ = Transfer.objects.get_or_create(
                agreement=agreement,
                kind=Transfer.Kind.RELEASE,
                defaults={
                    "status": Transfer.Status.PENDING,
                    "amount_minor": agreement.amount_minor,
                    "currency": agreement.currency,
                    "provider": Transfer.Provider.INTERNAL,
                    "provider_reference": f"release-{agreement.id}",
                    "idempotency_key": f"release-{agreement.id}",
                },
            )
            fee_minor = calculate_release_fee_minor(agreement.amount_minor, agreement.fee_bps)
            net_minor = agreement.amount_minor - fee_minor
            entries = [
                LedgerEntryInput.debit("ESCROW_LIABILITY", agreement.amount_minor, agreement.currency),
            ]
            if net_minor > 0:
                entries.append(
                    LedgerEntryInput.credit("ORGANIZATION_PAYABLE", net_minor, agreement.currency)
                )
            if fee_minor > 0:
                entries.append(
                    LedgerEntryInput.credit("PLATFORM_FEE_REVENUE", fee_minor, agreement.currency)
                )

            post_ledger_transaction(
                LedgerPosting(
                    transfer_id=release_transfer.id,
                    kind=LedgerTransaction.Kind.FUNDS_RELEASED,
                    currency=agreement.currency,
                    idempotency_key=f"dispute-released:{dispute.id}",
                    entries=tuple(entries),
                )
            )
            agreement.status = EscrowAgreement.Status.RELEASED
            agreement.version += 1
            agreement.realtime_sequence += 1
            agreement.save(update_fields=["status", "version", "realtime_sequence", "updated_at"])
            release_transfer.status = Transfer.Status.COMPLETED
            release_transfer.save(update_fields=["status", "updated_at"])

        elif decision == DisputeAdminDecision.Choice.REFUND_TO_CUSTOMER:
            refund_transfer, _ = Transfer.objects.get_or_create(
                agreement=agreement,
                kind=Transfer.Kind.REFUND,
                defaults={
                    "status": Transfer.Status.PENDING,
                    "amount_minor": agreement.amount_minor,
                    "currency": agreement.currency,
                    "provider": Transfer.Provider.INTERNAL,
                    "provider_reference": f"refund-{agreement.id}",
                    "idempotency_key": f"refund-{agreement.id}",
                },
            )
            post_ledger_transaction(
                LedgerPosting(
                    transfer_id=refund_transfer.id,
                    kind=LedgerTransaction.Kind.FUNDS_REFUNDED,
                    currency=agreement.currency,
                    idempotency_key=f"dispute-refunded:{dispute.id}",
                    entries=(
                        LedgerEntryInput.debit("ESCROW_LIABILITY", agreement.amount_minor, agreement.currency),
                        LedgerEntryInput.credit("PIX_CLEARING", agreement.amount_minor, agreement.currency),
                    ),
                )
            )
            agreement.status = EscrowAgreement.Status.REFUNDED
            agreement.version += 1
            agreement.realtime_sequence += 1
            agreement.save(update_fields=["status", "version", "realtime_sequence", "updated_at"])
            refund_transfer.status = Transfer.Status.COMPLETED
            refund_transfer.save(update_fields=["status", "updated_at"])

        admin_decision = DisputeAdminDecision.objects.create(
            dispute=dispute,
            recommendation=rec,
            admin=admin,
            command_id=norm_command_id,
            decision=decision,
            rationale=norm_rationale,
            decided_at=decided_at,
        )

        dispute.status = Dispute.Status.RESOLVED
        dispute.save(update_fields=["status", "updated_at"])

        enqueue_agreement_status_changed(
            agreement,
            correlation_id=eff_correlation_id,
            causation_id=str(admin_decision.id),
        )

        record_audit_event(
            event_type="dispute_admin_decision_resolved",
            organization=agreement.organization,
            agreement=agreement,
            actor=admin,
            correlation_id=eff_correlation_id,
            payload={
                "dispute_id": str(dispute.id),
                "decision_id": str(admin_decision.id),
                "decision": decision,
                "command_id": norm_command_id,
            },
        )

        return admin_decision, False


def _timestamp(now: datetime | None) -> datetime:
    if now is None:
        return timezone.now()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now


