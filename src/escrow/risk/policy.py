"""Versioned pure policy used before funds enter escrow custody."""

from __future__ import annotations

from collections.abc import Mapping
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
_RULE_NAMES = {
    "high_amount",
    "customer_velocity",
    "young_organization",
    "high_dispute_rate",
}


class FundingRiskPolicyConfigurationError(ValueError):
    """A stored policy snapshot cannot be evaluated safely."""


@dataclass(frozen=True)
class FundingRiskPolicyConfiguration:
    high_amount_minor: dict[Currency, int]
    customer_payments_last_60_seconds: int
    organization_age_days: int
    organization_dispute_rate_bps: int
    weights: dict[str, int]
    review_score_min: int
    reject_score_min: int


def default_funding_policy_configuration() -> dict[str, object]:
    """Return a fresh, JSON-safe snapshot of the documented MVP policy."""
    return {
        "high_amount_minor": {"BRL": 5_000_000, "USD": 1_000_000},
        "customer_payments_last_60_seconds": 3,
        "organization_age_days": 7,
        "organization_dispute_rate_bps": 1_000,
        "weights": {
            "high_amount": 25,
            "customer_velocity": 40,
            "young_organization": 15,
            "high_dispute_rate": 30,
        },
        "review_score_min": 40,
        "reject_score_min": 70,
    }


def evaluate_funding_policy(
    inputs: FundingRiskInputs,
    *,
    policy_version: str = POLICY_VERSION,
    configuration: Mapping[str, object] | None = None,
) -> FundingRiskResult:
    """Evaluate the documented score bands without side effects or hidden inputs."""
    settings = funding_policy_configuration_from(
        default_funding_policy_configuration() if configuration is None else configuration
    )
    if not isinstance(policy_version, str) or not policy_version or len(policy_version) > 64:
        raise FundingRiskPolicyConfigurationError("policy version is invalid")
    if inputs.organization_blocked:
        return FundingRiskResult(
            policy_version=policy_version,
            outcome=FundingRiskOutcome.REJECTED,
            score=0,
            reasons=("ORGANIZATION_BLOCKED",),
        )

    score = 0
    reasons: list[str] = []
    if inputs.amount_minor >= settings.high_amount_minor[inputs.currency]:
        score += settings.weights["high_amount"]
        reasons.append("HIGH_AMOUNT")
    if inputs.customer_payments_last_60_seconds >= settings.customer_payments_last_60_seconds:
        score += settings.weights["customer_velocity"]
        reasons.append("CUSTOMER_VELOCITY")
    if inputs.organization_age_days < settings.organization_age_days:
        score += settings.weights["young_organization"]
        reasons.append("YOUNG_ORGANIZATION")
    if inputs.organization_dispute_rate_bps > settings.organization_dispute_rate_bps:
        score += settings.weights["high_dispute_rate"]
        reasons.append("HIGH_DISPUTE_RATE")

    score = min(score, 100)
    if score >= settings.reject_score_min:
        outcome = FundingRiskOutcome.REJECTED
    elif score >= settings.review_score_min:
        outcome = FundingRiskOutcome.REVIEW_REQUIRED
    else:
        outcome = FundingRiskOutcome.APPROVED
    return FundingRiskResult(
        policy_version=policy_version,
        outcome=outcome,
        score=score,
        reasons=tuple(reasons),
    )


def funding_policy_configuration_from(
    configuration: Mapping[str, object],
) -> FundingRiskPolicyConfiguration:
    """Validate the complete persisted snapshot before it controls a decision."""
    expected_keys = {
        "high_amount_minor",
        "customer_payments_last_60_seconds",
        "organization_age_days",
        "organization_dispute_rate_bps",
        "weights",
        "review_score_min",
        "reject_score_min",
    }
    if set(configuration) != expected_keys:
        raise FundingRiskPolicyConfigurationError("policy configuration keys are invalid")
    high_amount_minor = _currency_thresholds(configuration["high_amount_minor"])
    weights = _weights(configuration["weights"])
    customer_payments_last_60_seconds = _positive_int(
        configuration["customer_payments_last_60_seconds"],
        "customer payment threshold",
    )
    organization_age_days = _positive_int(
        configuration["organization_age_days"],
        "organization age threshold",
    )
    organization_dispute_rate_bps = _non_negative_int(
        configuration["organization_dispute_rate_bps"],
        "organization dispute rate threshold",
    )
    review_score_min = _positive_int(
        configuration["review_score_min"], "review score threshold", maximum=100
    )
    reject_score_min = _positive_int(
        configuration["reject_score_min"], "reject score threshold", maximum=100
    )
    if not review_score_min < reject_score_min <= 100:
        raise FundingRiskPolicyConfigurationError("risk score bands are invalid")
    return FundingRiskPolicyConfiguration(
        high_amount_minor=high_amount_minor,
        customer_payments_last_60_seconds=customer_payments_last_60_seconds,
        organization_age_days=organization_age_days,
        organization_dispute_rate_bps=organization_dispute_rate_bps,
        weights=weights,
        review_score_min=review_score_min,
        reject_score_min=reject_score_min,
    )


def _currency_thresholds(value: object) -> dict[Currency, int]:
    if not isinstance(value, Mapping) or set(value) != {"BRL", "USD"}:
        raise FundingRiskPolicyConfigurationError("high amount thresholds are invalid")
    return {
        "BRL": _positive_int(value["BRL"], "BRL high amount threshold"),
        "USD": _positive_int(value["USD"], "USD high amount threshold"),
    }


def _weights(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != _RULE_NAMES:
        raise FundingRiskPolicyConfigurationError("risk rule weights are invalid")
    return {
        rule: _positive_int(value[rule], f"{rule} weight", maximum=100)
        for rule in sorted(_RULE_NAMES)
    }


def _positive_int(value: object, label: str, *, maximum: int = 9_223_372_036_854_775_807) -> int:
    if type(value) is not int or value < 1 or value > maximum:
        raise FundingRiskPolicyConfigurationError(f"{label} is invalid")
    return value


def _non_negative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0 or value > 10_000:
        raise FundingRiskPolicyConfigurationError(f"{label} is invalid")
    return value
