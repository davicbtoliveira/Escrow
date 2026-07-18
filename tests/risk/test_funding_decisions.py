from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.organizations.models import Organization
from escrow.payments.models import Transfer
from escrow.risk.models import FundingRiskDecision, FundingRiskPolicy
from escrow.risk.policy import FundingRiskOutcome
from escrow.risk.services import evaluate_funding_transfer, funding_policy_configuration


def funded_transfer(*, blocked: bool = False, amount_minor: int = 50_000) -> Transfer:
    organization = Organization.objects.create(name="Loja de Risco", risk_blocked=blocked)
    Organization.objects.filter(id=organization.id).update(
        created_at=timezone.now() - timedelta(days=30)
    )
    agreement = EscrowAgreement.objects.create(
        organization=organization,
        external_customer_id="risk-buyer-001",
        customer_name_masked="A***",
        customer_email_masked="a***@example.test",
        customer_document_masked="***.***.***-25",
        customer_document_kind="CPF",
        customer_email_blind_index="a" * 64,
        customer_document_blind_index="b" * 64,
        customer_pii_ciphertext=b"ciphertext",
        customer_pii_nonce=b"nonce",
        customer_pii_encrypted_data_key=b"encrypted-key",
        customer_pii_kms_key_id="local-test-key",
        checkout_token_hash="c" * 64,
        amount_minor=amount_minor,
        currency="BRL",
        fee_bps=200,
        delivery_window_days=7,
    )
    return Transfer.objects.create(
        agreement=agreement,
        kind=Transfer.Kind.FUNDING,
        amount_minor=amount_minor,
        currency="BRL",
        provider=Transfer.Provider.SANDBOX_PIX,
        provider_reference=f"pix-risk-{agreement.id}",
        idempotency_key="risk-funding-001",
    )


class FundingRiskDecisionTests(TestCase):
    def test_persists_explainable_approved_decision_once(self) -> None:
        transfer = funded_transfer()

        first = evaluate_funding_transfer(transfer.id, now=timezone.now())
        replay = evaluate_funding_transfer(transfer.id, now=timezone.now())

        assert first.id == replay.id
        assert first.outcome == FundingRiskOutcome.APPROVED
        assert first.policy_version == "funding-risk-v1"
        assert first.score == 0
        assert first.inputs == {
            "amount_minor": 50_000,
            "currency": "BRL",
            "customer_payments_last_60_seconds": 0,
            "organization_age_days": 30,
            "organization_dispute_rate_bps": 0,
            "organization_blocked": False,
        }
        assert FundingRiskDecision.objects.count() == 1

    def test_blocked_organization_is_persisted_as_rejected(self) -> None:
        transfer = funded_transfer(blocked=True)

        decision = evaluate_funding_transfer(transfer.id, now=timezone.now())

        assert decision.outcome == FundingRiskOutcome.REJECTED
        assert decision.reasons == ["ORGANIZATION_BLOCKED"]

    def test_persists_the_versioned_policy_snapshot_for_reproducible_replays(self) -> None:
        transfer = funded_transfer(amount_minor=50_000)
        configuration = funding_policy_configuration()
        high_amount_minor = configuration["high_amount_minor"]
        assert isinstance(high_amount_minor, dict)
        high_amount_minor["BRL"] = 50_000
        weights = configuration["weights"]
        assert isinstance(weights, dict)
        weights["high_amount"] = 40
        policy = FundingRiskPolicy.objects.create(
            version="funding-risk-v2",
            configuration=configuration,
        )

        first = evaluate_funding_transfer(
            transfer.id,
            now=timezone.now(),
            policy_version=policy.version,
        )
        replay = evaluate_funding_transfer(transfer.id, now=timezone.now())

        assert first.id == replay.id
        assert first.policy_id == policy.id
        assert first.policy_version == "funding-risk-v2"
        assert first.policy_configuration == configuration
        assert first.score == 40
        assert first.outcome == FundingRiskOutcome.REVIEW_REQUIRED
