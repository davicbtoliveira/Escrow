"""Versioned pure policy used before funds enter escrow custody."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

Currency = Literal["BRL", "USD"]


class FundingRiskOutcome(StrEnum):
    APPROVED = "APPROVED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class FundingRiskInputs:
    amount_minor: int
    currency: Currency
    customer_payments_last_60_seconds: int
    organization_age_days: int
    organization_dispute_rate_bps: int
    organization_blocked: bool


@dataclass(frozen=True)
class FundingRiskResult:
    policy_version: str
    outcome: FundingRiskOutcome
    score: int
    reasons: tuple[str, ...]


POLICY_VERSION = "funding-risk-v1"
_HIGH_AMOUNT_MINOR: dict[Currency, int] = {"BRL": 5_000_000, "USD": 1_000_000}


def evaluate_funding_policy(inputs: FundingRiskInputs) -> FundingRiskResult:
    """Evaluate the documented score bands without side effects or hidden inputs."""
    if inputs.organization_blocked:
        return FundingRiskResult(
            policy_version=POLICY_VERSION,
            outcome=FundingRiskOutcome.REJECTED,
            score=0,
            reasons=("ORGANIZATION_BLOCKED",),
        )

    score = 0
    reasons: list[str] = []
    if inputs.amount_minor >= _HIGH_AMOUNT_MINOR[inputs.currency]:
        score += 25
        reasons.append("HIGH_AMOUNT")
    if inputs.customer_payments_last_60_seconds >= 3:
        score += 40
        reasons.append("CUSTOMER_VELOCITY")
    if inputs.organization_age_days < 7:
        score += 15
        reasons.append("YOUNG_ORGANIZATION")
    if inputs.organization_dispute_rate_bps > 1_000:
        score += 30
        reasons.append("HIGH_DISPUTE_RATE")

    if score >= 70:
        outcome = FundingRiskOutcome.REJECTED
    elif score >= 40:
        outcome = FundingRiskOutcome.REVIEW_REQUIRED
    else:
        outcome = FundingRiskOutcome.APPROVED
    return FundingRiskResult(
        policy_version=POLICY_VERSION,
        outcome=outcome,
        score=score,
        reasons=tuple(reasons),
    )
