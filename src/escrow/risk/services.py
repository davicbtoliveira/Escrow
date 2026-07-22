"""Persisted funding-risk evaluation without decrypting customer identity."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid5

from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone

from escrow.agreements.lifecycle import AgreementStateConflict, resume_funding_after_review
from escrow.agreements.models import EscrowAgreement
from escrow.audit.services import record_audit_event
from escrow.disputes.models import Dispute, Evidence
from escrow.identity.models import User
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.models import OutboxEvent
from escrow.messaging.outbox import enqueue_outbox_event
from escrow.messaging.topology import LEDGER_FUNDING_QUEUE, LEDGER_REFUND_QUEUE
from escrow.notifications.outbox import enqueue_agreement_status_changed
from escrow.payments.models import SandboxPixCharge, Transfer
from escrow.risk.models import (
    DisputeRiskPolicy,
    DisputeRiskReport,
    FundingRiskDecision,
    FundingRiskPolicy,
    FundingRiskReview,
)
from escrow.risk.policy import (
    POLICY_VERSION,
    Currency,
    FundingRiskInputs,
    FundingRiskOutcome,
    FundingRiskPolicyConfigurationError,
    default_funding_policy_configuration,
    evaluate_funding_policy,
)



class FundingRiskPolicyNotFound(RuntimeError):
    """A requested historical policy version has not been provisioned."""


RISK_DISPUTE_ANALYST_GROUP = "RISK_DISPUTE_ANALYST"
PLATFORM_ADMIN_GROUP = "PLATFORM_ADMIN"
_MANUAL_POST_FUNDING_NAMESPACE = UUID("1e4e8437-8924-49cc-a62a-f4dfb8af0fe9")
_MANUAL_RETURN_FUNDING_NAMESPACE = UUID("3e5dff29-bddf-4f16-9de3-9d6870707263")


class FundingReviewAuthorizationError(PermissionError):
    """The actor does not hold the distinct risk-analyst capability."""


class FundingReviewAlreadyResolved(RuntimeError):
    """Another review command already resolved this policy decision."""


class FundingReviewIdempotencyConflict(RuntimeError):
    """A command identifier was replayed with different reviewer intent."""


class FundingReviewNotPending(RuntimeError):
    """Only policy decisions requiring human review may be resolved."""


class FundingReviewValidationError(ValueError):
    """A review command has malformed, non-auditable input."""


@dataclass(frozen=True, slots=True)
class FundingReviewQueueItem:
    """Safe analyst projection: it deliberately excludes encrypted customer identity."""

    decision_id: UUID
    transfer_id: UUID
    agreement_id: UUID
    organization_id: UUID
    organization_name_masked: str
    amount_minor: int
    currency: str
    customer_name_masked: str
    customer_email_masked: str
    customer_document_masked: str
    policy_version: str
    policy_configuration: dict[str, object]
    inputs: dict[str, object]
    score: int
    reasons: tuple[str, ...]
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class FundingReviewResolution:
    review: FundingRiskReview
    decision: FundingRiskDecision
    replayed: bool


def evaluate_funding_transfer(
    transfer_id: UUID | str,
    *,
    now: datetime | None = None,
    policy_version: str = POLICY_VERSION,
) -> FundingRiskDecision:
    """Store the first deterministic decision for a confirmed funding transfer."""
    evaluated_at = timezone.now() if now is None else now
    with transaction.atomic():
        transfer = (
            Transfer.objects.select_for_update()
            .select_related("agreement__organization")
            .get(id=transfer_id)
        )
        existing = (
            FundingRiskDecision.objects.select_related("policy").filter(transfer=transfer).first()
        )
        if existing is not None:
            return existing
        policy = _policy_for_version(policy_version)
        if not isinstance(policy.configuration, dict):
            raise FundingRiskPolicyConfigurationError("stored policy configuration is invalid")
        configuration = deepcopy(policy.configuration)
        inputs = _inputs_for_transfer(transfer, evaluated_at)
        result = evaluate_funding_policy(
            inputs,
            policy_version=policy.version,
            configuration=configuration,
        )
        return FundingRiskDecision.objects.create(
            transfer=transfer,
            policy=policy,
            policy_version=result.policy_version,
            policy_configuration=configuration,
            inputs=_serializable_inputs(inputs),
            score=result.score,
            reasons=list(result.reasons),
            outcome=result.outcome,
            evaluated_at=evaluated_at,
        )


def evaluate_dispute_risk_service(
    dispute_id: UUID | str,
    *,
    correlation_id: str = "",
    now: datetime | None = None,
) -> DisputeRiskReport:
    """Generate an explainable risk report for an opened dispute."""
    evaluated_at = timezone.now() if now is None else now
    dispute_uuid = _uuid_value(dispute_id, "dispute id")
    effective_correlation_id = (
        correlation_id.strip() if correlation_id and correlation_id.strip() else f"dispute-risk-{dispute_uuid}"
    )
    with transaction.atomic():
        dispute = (
            Dispute.objects.select_for_update()
            .select_related("agreement__organization")
            .get(id=dispute_uuid)
        )
        existing = (
            DisputeRiskReport.objects.select_related("policy")
            .filter(dispute=dispute)
            .first()
        )
        if existing is not None:
            return existing

        policy, _ = DisputeRiskPolicy.objects.get_or_create(
            version="v1",
            defaults={
                "configuration": {
                    "duplicate_evidence_score": 30,
                    "frequent_customer_disputes_score": 25,
                    "rapid_dispute_score": 20,
                    "high_organization_dispute_rate_score": 25,
                }
            },
        )

        from escrow.delivery.models import DeliveryReport

        evidences = Evidence.objects.filter(dispute=dispute)
        evidence_hashes = list(evidences.values_list("sha256", flat=True))
        duplicate_hashes = list(
            Evidence.objects.filter(sha256__in=evidence_hashes)
            .exclude(dispute=dispute)
            .values_list("sha256", flat=True)
            .distinct()
        )

        customer_disputes_count = (
            Dispute.objects.filter(
                agreement__customer_document_blind_index=dispute.agreement.customer_document_blind_index
            )
            .exclude(id=dispute.id)
            .count()
        )

        org_total = EscrowAgreement.objects.filter(
            organization=dispute.agreement.organization,
            created_at__gte=evaluated_at - timedelta(days=30),
        ).count()

        org_disputes = Dispute.objects.filter(
            agreement__organization=dispute.agreement.organization,
            opened_at__gte=evaluated_at - timedelta(days=30),
        ).count()

        org_dispute_rate_bps = (
            int((org_disputes / org_total) * 10_000) if org_total > 0 else 0
        )

        delivery_report = DeliveryReport.objects.filter(agreement=dispute.agreement).first()

        score = 0
        flags: list[str] = []

        if duplicate_hashes:
            score += 30
            flags.append("duplicate_evidence_detected")

        if customer_disputes_count >= 2:
            score += 25
            flags.append("customer_frequent_disputes")

        if org_dispute_rate_bps > 1_000 and org_total >= 3:
            score += 25
            flags.append("high_organization_dispute_rate")

        if delivery_report and (
            dispute.opened_at - delivery_report.reported_at
        ) < timedelta(minutes=1):
            score += 20
            flags.append("rapid_dispute_after_delivery")

        if score == 0 and not flags:
            suspicion_result = DisputeRiskReport.SuspicionResult.NO_SUSPICION
            summary = "No suspicious risk indicators detected. The customer submitted evidence for analyst review."
        else:
            suspicion_result = DisputeRiskReport.SuspicionResult.SUSPICIOUS_INDICATORS
            summary = f"Risk indicators detected: {', '.join(flags)}."

        timeline = [
            {"event": "agreement_created", "timestamp": dispute.agreement.created_at.isoformat()},
        ]
        if delivery_report:
            timeline.append(
                {"event": "delivery_reported", "timestamp": delivery_report.reported_at.isoformat()}
            )
        timeline.append({"event": "dispute_opened", "timestamp": dispute.opened_at.isoformat()})

        report = DisputeRiskReport.objects.create(
            dispute=dispute,
            policy=policy,
            policy_version=policy.version,
            policy_configuration=policy.configuration,
            inputs={
                "customer_disputes_count": customer_disputes_count,
                "organization_dispute_rate_bps": org_dispute_rate_bps,
                "organization_agreements_30d": org_total,
                "duplicate_hashes": duplicate_hashes,
            },
            summary=summary,
            timeline=timeline,
            customer_history={"prior_disputes_count": customer_disputes_count},
            organization_history={
                "total_agreements_30d": org_total,
                "disputes_count_30d": org_disputes,
                "dispute_rate_bps": org_dispute_rate_bps,
            },
            evidence_integrity={
                "uploaded_count": len(evidence_hashes),
                "duplicate_hashes_detected": duplicate_hashes,
            },
            score=min(100, score),
            flags=flags,
            suspicion_result=suspicion_result,
            generated_at=evaluated_at,
        )

        dispute.status = Dispute.Status.ANALYST_REVIEW
        dispute.save(update_fields=["status", "updated_at"])

        enqueue_agreement_status_changed(
            dispute.agreement,
            correlation_id=effective_correlation_id,
            causation_id=str(report.id),
        )

        record_audit_event(
            event_type="dispute_risk_report_generated",
            organization=dispute.agreement.organization,
            agreement=dispute.agreement,
            correlation_id=effective_correlation_id,
            payload={
                "dispute_id": str(dispute.id),
                "report_id": str(report.id),
                "suspicion_result": suspicion_result,
            },
        )
        return report



def funding_policy_configuration() -> dict[str, Any]:
    """Expose the immutable v1 thresholds alongside each persisted policy version."""
    return default_funding_policy_configuration()


def _policy_for_version(version: str) -> FundingRiskPolicy:
    if version == POLICY_VERSION:
        policy, _ = FundingRiskPolicy.objects.get_or_create(
            version=POLICY_VERSION,
            defaults={"configuration": funding_policy_configuration()},
        )
        return policy
    try:
        return FundingRiskPolicy.objects.get(version=version)
    except FundingRiskPolicy.DoesNotExist as error:
        raise FundingRiskPolicyNotFound("funding risk policy does not exist") from error


def list_manual_funding_review_queue(
    *,
    analyst: User,
    organization_id: UUID | str | None = None,
) -> list[FundingReviewQueueItem]:
    """Return only masked, unresolved reviews after checking the analyst role."""
    require_risk_dispute_analyst(analyst)
    queryset = _pending_review_queryset()
    if organization_id is not None:
        queryset = queryset.filter(
            transfer__agreement__organization_id=_uuid_value(organization_id, "organization id")
        )
    return [_queue_item_from_decision(decision) for decision in queryset]


def resolve_funding_review(
    *,
    decision_id: UUID | str,
    analyst: User,
    outcome: FundingRiskReview.Outcome | str,
    command_id: str,
    rationale: str,
    correlation_id: str = "",
    now: datetime | None = None,
) -> FundingReviewResolution:
    """Persist exactly one analyst outcome and its immutable audit fact.

    Callers that enqueue custody or return commands should wrap this service in
    their outer transaction so the review and its next command commit together.
    """
    require_risk_dispute_analyst(analyst)
    normalized_outcome = _review_outcome(outcome)
    normalized_command_id = _non_blank_text(command_id, "command id", maximum=128)
    normalized_rationale = _non_blank_text(rationale, "rationale", maximum=1_000)
    if not isinstance(correlation_id, str) or len(correlation_id) > 128:
        raise FundingReviewValidationError("correlation id is invalid")
    reviewed_at = timezone.now() if now is None else now
    if reviewed_at.tzinfo is None:
        raise FundingReviewValidationError("review time must be timezone-aware")

    with transaction.atomic():
        decision = _locked_decision_for_review(decision_id)
        existing = FundingRiskReview.objects.select_for_update().filter(decision=decision).first()
        if existing is not None:
            return _replay_or_raise(
                existing,
                decision=decision,
                analyst=analyst,
                outcome=normalized_outcome,
                command_id=normalized_command_id,
                rationale=normalized_rationale,
            )
        if decision.outcome != FundingRiskOutcome.REVIEW_REQUIRED:
            raise FundingReviewNotPending("funding decision does not require manual review")
        if (
            decision.transfer.agreement.organization.risk_blocked
            and normalized_outcome == FundingRiskReview.Outcome.APPROVED
        ):
            raise FundingReviewNotPending("blocked organization funding cannot be approved")
        try:
            with transaction.atomic():
                review = FundingRiskReview.objects.create(
                    decision=decision,
                    analyst=analyst,
                    command_id=normalized_command_id,
                    outcome=normalized_outcome,
                    rationale=normalized_rationale,
                    reviewed_at=reviewed_at,
                )
        except IntegrityError:
            existing = (
                FundingRiskReview.objects.select_for_update().filter(decision=decision).first()
            )
            if existing is None:
                if FundingRiskReview.objects.filter(command_id=normalized_command_id).exists():
                    raise FundingReviewIdempotencyConflict(
                        "review command id is already attached to another decision"
                    ) from None
                raise
            return _replay_or_raise(
                existing,
                decision=decision,
                analyst=analyst,
                outcome=normalized_outcome,
                command_id=normalized_command_id,
                rationale=normalized_rationale,
            )
        record_audit_event(
            event_type="funding_risk_review_resolved",
            organization=decision.transfer.agreement.organization,
            agreement=decision.transfer.agreement,
            actor=analyst,
            correlation_id=correlation_id,
            payload={
                "command_id": normalized_command_id,
                "decision_id": str(decision.id),
                "outcome": normalized_outcome,
                "review_id": str(review.id),
                "transfer_id": str(decision.transfer_id),
            },
        )
        return FundingReviewResolution(review=review, decision=decision, replayed=False)


def resolve_funding_review_and_enqueue(
    *,
    decision_id: UUID | str,
    analyst: User,
    outcome: FundingRiskReview.Outcome | str,
    command_id: str,
    rationale: str,
    correlation_id: str,
    now: datetime | None = None,
) -> FundingReviewResolution:
    """Resolve one human review and atomically emit its custody or return command."""
    with transaction.atomic():
        resolution = resolve_funding_review(
            decision_id=decision_id,
            analyst=analyst,
            outcome=outcome,
            command_id=command_id,
            rationale=rationale,
            correlation_id=correlation_id,
            now=now,
        )
        if resolution.replayed:
            return resolution
        transfer = resolution.decision.transfer
        agreement = transfer.agreement
        if resolution.review.outcome == FundingRiskReview.Outcome.APPROVED:
            try:
                agreement = resume_funding_after_review(agreement.id)
            except AgreementStateConflict as error:
                raise FundingReviewNotPending("funding review transition is stale") from error
            _enqueue_manual_funding_command(
                transfer=transfer,
                agreement=agreement,
                message_id=uuid5(_MANUAL_POST_FUNDING_NAMESPACE, str(transfer.id)),
                message_type="PostFunding.v1",
                routing_key=LEDGER_FUNDING_QUEUE.name,
                correlation_id=correlation_id,
                causation_id=str(resolution.review.id),
                occurred_at=resolution.review.reviewed_at,
            )
            enqueue_agreement_status_changed(
                agreement,
                correlation_id=correlation_id,
                causation_id=str(resolution.review.id),
            )
        else:
            _enqueue_manual_funding_command(
                transfer=transfer,
                agreement=agreement,
                message_id=uuid5(_MANUAL_RETURN_FUNDING_NAMESPACE, str(transfer.id)),
                message_type="ReturnRejectedFunding.v1",
                routing_key=LEDGER_REFUND_QUEUE.name,
                correlation_id=correlation_id,
                causation_id=str(resolution.review.id),
                occurred_at=resolution.review.reviewed_at,
            )
        return resolution


def _enqueue_manual_funding_command(
    *,
    transfer: Transfer,
    agreement: EscrowAgreement,
    message_id: UUID,
    message_type: str,
    routing_key: str,
    correlation_id: str,
    causation_id: str,
    occurred_at: datetime,
) -> None:
    if OutboxEvent.objects.filter(id=message_id).exists():
        return
    enqueue_outbox_event(
        MessageEnvelope.build(
            message_id=message_id,
            message_type=message_type,
            version=1,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            causation_id=causation_id,
            tenant_id=str(agreement.organization_id),
            payload={"agreement_id": str(agreement.id), "transfer_id": str(transfer.id)},
        ),
        routing_key=routing_key,
    )


def require_risk_dispute_analyst(analyst: User) -> None:
    """Enforce an explicit, non-admin analyst role with no implicit bypass."""
    if not analyst.is_active or not analyst.is_staff:
        raise FundingReviewAuthorizationError("risk analyst capability is required")
    roles = set(
        analyst.groups.filter(
            name__in=[RISK_DISPUTE_ANALYST_GROUP, PLATFORM_ADMIN_GROUP]
        ).values_list("name", flat=True)
    )
    if RISK_DISPUTE_ANALYST_GROUP not in roles or PLATFORM_ADMIN_GROUP in roles:
        raise FundingReviewAuthorizationError("risk analyst capability is required")


def _pending_review_queryset() -> QuerySet[FundingRiskDecision]:
    return (
        FundingRiskDecision.objects.filter(
            outcome=FundingRiskOutcome.REVIEW_REQUIRED,
            manual_review__isnull=True,
        )
        .select_related("transfer__agreement__organization")
        .only(
            "id",
            "transfer_id",
            "policy_version",
            "policy_configuration",
            "inputs",
            "score",
            "reasons",
            "evaluated_at",
            "transfer__id",
            "transfer__agreement_id",
            "transfer__amount_minor",
            "transfer__currency",
            "transfer__agreement__id",
            "transfer__agreement__organization_id",
            "transfer__agreement__customer_name_masked",
            "transfer__agreement__customer_email_masked",
            "transfer__agreement__customer_document_masked",
            "transfer__agreement__organization__id",
            "transfer__agreement__organization__name",
        )
        .order_by("evaluated_at", "id")
    )


def _queue_item_from_decision(decision: FundingRiskDecision) -> FundingReviewQueueItem:
    agreement = decision.transfer.agreement
    organization = agreement.organization
    return FundingReviewQueueItem(
        decision_id=decision.id,
        transfer_id=decision.transfer_id,
        agreement_id=agreement.id,
        organization_id=organization.id,
        organization_name_masked=_masked_text(organization.name),
        amount_minor=decision.transfer.amount_minor,
        currency=decision.transfer.currency,
        customer_name_masked=agreement.customer_name_masked,
        customer_email_masked=agreement.customer_email_masked,
        customer_document_masked=agreement.customer_document_masked,
        policy_version=decision.policy_version,
        policy_configuration=_json_object(decision.policy_configuration),
        inputs=_json_object(decision.inputs),
        score=decision.score,
        reasons=tuple(decision.reasons),
        evaluated_at=decision.evaluated_at,
    )


def _locked_decision_for_review(decision_id: UUID | str) -> FundingRiskDecision:
    try:
        return (
            FundingRiskDecision.objects.select_for_update()
            .select_related("transfer__agreement__organization")
            .get(id=_uuid_value(decision_id, "decision id"))
        )
    except FundingRiskDecision.DoesNotExist as error:
        raise FundingReviewNotPending("funding decision does not exist") from error


def _replay_or_raise(
    review: FundingRiskReview,
    *,
    decision: FundingRiskDecision,
    analyst: User,
    outcome: str,
    command_id: str,
    rationale: str,
) -> FundingReviewResolution:
    if review.command_id != command_id:
        raise FundingReviewAlreadyResolved("funding review is already resolved")
    if (
        review.analyst_id != analyst.id
        or review.outcome != outcome
        or review.rationale != rationale
    ):
        raise FundingReviewIdempotencyConflict("review command intent differs from its first use")
    return FundingReviewResolution(review=review, decision=decision, replayed=True)


def _review_outcome(value: FundingRiskReview.Outcome | str) -> str:
    if value not in FundingRiskReview.Outcome.values:
        raise FundingReviewValidationError("review outcome is invalid")
    return str(value)


def _uuid_value(value: UUID | str, label: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(value)
    except (TypeError, ValueError) as error:
        raise FundingReviewValidationError(f"{label} is invalid") from error


def _non_blank_text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise FundingReviewValidationError(f"{label} is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise FundingReviewValidationError(f"{label} is invalid")
    return normalized


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError("persisted risk snapshot is invalid")
    return cast(dict[str, object], deepcopy(value))


def _masked_text(value: str) -> str:
    return f"{value[:1]}***" if value else "***"


def _inputs_for_transfer(transfer: Transfer, evaluated_at: datetime) -> FundingRiskInputs:
    agreement = transfer.agreement
    organization = agreement.organization
    velocity = SandboxPixCharge.objects.filter(
        agreement__customer_document_blind_index=agreement.customer_document_blind_index,
        confirmed_at__gte=evaluated_at - timedelta(seconds=60),
        confirmed_at__lte=evaluated_at,
    ).count()
    if transfer.currency not in {"BRL", "USD"}:
        raise RuntimeError("funding transfer currency is invalid")
    return FundingRiskInputs(
        amount_minor=transfer.amount_minor,
        currency=cast(Currency, transfer.currency),
        customer_payments_last_60_seconds=velocity,
        organization_age_days=max(0, (evaluated_at - organization.created_at).days),
        organization_dispute_rate_bps=0,
        organization_blocked=organization.risk_blocked,
    )


def _serializable_inputs(inputs: FundingRiskInputs) -> dict[str, object]:
    return {
        "amount_minor": inputs.amount_minor,
        "currency": inputs.currency,
        "customer_payments_last_60_seconds": inputs.customer_payments_last_60_seconds,
        "organization_age_days": inputs.organization_age_days,
        "organization_dispute_rate_bps": inputs.organization_dispute_rate_bps,
        "organization_blocked": inputs.organization_blocked,
    }
