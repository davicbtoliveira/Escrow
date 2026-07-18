"""Persisted funding-risk evaluation without decrypting customer identity."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from escrow.payments.models import SandboxPixCharge, Transfer
from escrow.risk.models import FundingRiskDecision, FundingRiskPolicy
from escrow.risk.policy import (
    POLICY_VERSION,
    Currency,
    FundingRiskInputs,
    evaluate_funding_policy,
)


def evaluate_funding_transfer(
    transfer_id: UUID | str,
    *,
    now: datetime | None = None,
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
        policy, _ = FundingRiskPolicy.objects.get_or_create(
            version=POLICY_VERSION,
            defaults={"configuration": funding_policy_configuration()},
        )
        inputs = _inputs_for_transfer(transfer, evaluated_at)
        result = evaluate_funding_policy(inputs)
        return FundingRiskDecision.objects.create(
            transfer=transfer,
            policy=policy,
            policy_version=result.policy_version,
            inputs=_serializable_inputs(inputs),
            score=result.score,
            reasons=list(result.reasons),
            outcome=result.outcome,
            evaluated_at=evaluated_at,
        )


def funding_policy_configuration() -> dict[str, Any]:
    """Expose the immutable v1 thresholds alongside each persisted policy version."""
    return {
        "high_amount_minor": {"BRL": 5_000_000, "USD": 1_000_000},
        "customer_payments_last_60_seconds": 3,
        "organization_age_days": 7,
        "organization_dispute_rate_bps": 1_000,
        "review_score_min": 40,
        "reject_score_min": 70,
    }


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
