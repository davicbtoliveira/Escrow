from __future__ import annotations

from escrow.risk.policy import FundingRiskInputs, FundingRiskOutcome, evaluate_funding_policy


def inputs(**overrides: object) -> FundingRiskInputs:
    defaults: dict[str, object] = {
        "amount_minor": 10_000,
        "currency": "BRL",
        "customer_payments_last_60_seconds": 0,
        "organization_age_days": 30,
        "organization_dispute_rate_bps": 0,
        "organization_blocked": False,
    }
    defaults.update(overrides)
    return FundingRiskInputs(**defaults)  # type: ignore[arg-type]


def test_policy_approves_without_indicators() -> None:
    result = evaluate_funding_policy(inputs())

    assert result.outcome is FundingRiskOutcome.APPROVED
    assert result.score == 0
    assert result.reasons == ()
    assert result.policy_version == "funding-risk-v1"


def test_policy_uses_exact_amount_and_velocity_boundaries() -> None:
    high_amount = evaluate_funding_policy(inputs(amount_minor=5_000_000))
    review = evaluate_funding_policy(
        inputs(amount_minor=5_000_000, customer_payments_last_60_seconds=3)
    )

    assert high_amount.score == 25
    assert high_amount.outcome is FundingRiskOutcome.APPROVED
    assert high_amount.reasons == ("HIGH_AMOUNT",)
    assert review.score == 65
    assert review.outcome is FundingRiskOutcome.REVIEW_REQUIRED
    assert review.reasons == ("HIGH_AMOUNT", "CUSTOMER_VELOCITY")


def test_policy_handles_usd_age_and_dispute_rate_boundaries() -> None:
    usd = evaluate_funding_policy(inputs(amount_minor=1_000_000, currency="USD"))
    age_boundary = evaluate_funding_policy(inputs(organization_age_days=7))
    young = evaluate_funding_policy(inputs(organization_age_days=6))
    dispute_boundary = evaluate_funding_policy(inputs(organization_dispute_rate_bps=1_000))
    dispute_high = evaluate_funding_policy(inputs(organization_dispute_rate_bps=1_001))

    assert usd.score == 25
    assert age_boundary.score == 0
    assert young.score == 15
    assert dispute_boundary.score == 0
    assert dispute_high.score == 30


def test_policy_rejects_blocked_organizations_immediately() -> None:
    result = evaluate_funding_policy(
        inputs(
            amount_minor=5_000_000,
            customer_payments_last_60_seconds=3,
            organization_blocked=True,
        )
    )

    assert result.outcome is FundingRiskOutcome.REJECTED
    assert result.score == 0
    assert result.reasons == ("ORGANIZATION_BLOCKED",)
