from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.audit.models import AuditEvent
from escrow.messaging.models import OutboxEvent
from escrow.organizations.models import Organization
from escrow.payments.models import Transfer
from escrow.risk.models import FundingRiskDecision, FundingRiskReview
from escrow.risk.policy import FundingRiskOutcome
from escrow.risk.services import (
    PLATFORM_ADMIN_GROUP,
    RISK_DISPUTE_ANALYST_GROUP,
    FundingReviewAlreadyResolved,
    FundingReviewAuthorizationError,
    FundingReviewNotPending,
    evaluate_funding_transfer,
    list_manual_funding_review_queue,
    resolve_funding_review,
    resolve_funding_review_and_enqueue,
)


def review_required_decision(
    *, organization_name: str = "Loja para análise"
) -> FundingRiskDecision:
    organization = Organization.objects.create(name=organization_name)
    now = timezone.now()
    Organization.objects.filter(id=organization.id).update(created_at=now - timedelta(days=6))
    agreement = EscrowAgreement.objects.create(
        organization=organization,
        external_customer_id=f"buyer-{uuid4().hex}",
        customer_name_masked="A*** S****",
        customer_email_masked="a***@example.test",
        customer_document_masked="***.***.***-25",
        customer_document_kind="CPF",
        customer_email_blind_index=uuid4().hex * 2,
        customer_document_blind_index=uuid4().hex * 2,
        customer_pii_ciphertext=b"ciphertext",
        customer_pii_nonce=b"nonce",
        customer_pii_encrypted_data_key=b"encrypted-key",
        customer_pii_kms_key_id="local-test-key",
        checkout_token_hash=uuid4().hex * 2,
        amount_minor=5_000_000,
        currency="BRL",
        fee_bps=200,
        delivery_window_days=7,
    )
    transfer = Transfer.objects.create(
        agreement=agreement,
        kind=Transfer.Kind.FUNDING,
        amount_minor=agreement.amount_minor,
        currency=agreement.currency,
        provider=Transfer.Provider.SANDBOX_PIX,
        provider_reference=f"pix-review-{agreement.id}",
        idempotency_key=f"risk-review-{agreement.id}",
    )
    decision = evaluate_funding_transfer(transfer.id, now=now)
    assert decision.outcome == FundingRiskOutcome.REVIEW_REQUIRED
    return decision


def risk_analyst() -> object:
    user = get_user_model().objects.create_user(
        email=f"analyst-{uuid4().hex}@example.test",
        password="Uma senha forte e exclusiva 2026!",
        is_staff=True,
    )
    group, _ = Group.objects.get_or_create(name=RISK_DISPUTE_ANALYST_GROUP)
    user.groups.add(group)
    return user


class FundingRiskReviewTests(TestCase):
    def test_analyst_queue_is_tenant_filtered_and_exposes_only_masked_customer_data(self) -> None:
        first = review_required_decision(organization_name="Primeira organização")
        second = review_required_decision(organization_name="Segunda organização")

        items = list_manual_funding_review_queue(
            analyst=risk_analyst(),
            organization_id=first.transfer.agreement.organization_id,
        )

        assert [item.decision_id for item in items] == [first.id]
        assert items[0].organization_name_masked == "P***"
        assert items[0].customer_name_masked == "A*** S****"
        assert items[0].customer_email_masked == "a***@example.test"
        assert items[0].customer_document_masked == "***.***.***-25"
        assert items[0].score == 40
        assert items[0].reasons == ("HIGH_AMOUNT", "YOUNG_ORGANIZATION")
        assert second.id not in {item.decision_id for item in items}
        assert not hasattr(items[0], "customer_pii_ciphertext")

    def test_only_an_internal_risk_analyst_can_read_the_manual_queue(self) -> None:
        review_required_decision()
        unauthorized_user = get_user_model().objects.create_user(
            email="staff-without-risk-role@example.test",
            password="Uma senha forte e exclusiva 2026!",
            is_staff=True,
        )

        with self.assertRaises(FundingReviewAuthorizationError):
            list_manual_funding_review_queue(analyst=unauthorized_user)

    def test_platform_admin_group_cannot_be_used_as_an_analyst_bypass(self) -> None:
        review_required_decision()
        platform_admin = get_user_model().objects.create_user(
            email="platform-admin@example.test",
            password="Uma senha forte e exclusiva 2026!",
            is_staff=True,
        )
        group, _ = Group.objects.get_or_create(name=PLATFORM_ADMIN_GROUP)
        platform_admin.groups.add(group)

        with self.assertRaises(FundingReviewAuthorizationError):
            list_manual_funding_review_queue(analyst=platform_admin)

    def test_analyst_resolution_is_audited_and_idempotent_per_command(self) -> None:
        decision = review_required_decision()
        analyst = risk_analyst()

        first = resolve_funding_review(
            decision_id=decision.id,
            analyst=analyst,
            outcome=FundingRiskReview.Outcome.APPROVED,
            command_id="funding-review-command-001",
            rationale="Os sinais são explicados por uma campanha de lançamento.",
            correlation_id="risk-review-correlation-001",
        )
        replay = resolve_funding_review(
            decision_id=decision.id,
            analyst=analyst,
            outcome=FundingRiskReview.Outcome.APPROVED,
            command_id="funding-review-command-001",
            rationale="Os sinais são explicados por uma campanha de lançamento.",
            correlation_id="risk-review-correlation-001",
        )

        assert not first.replayed
        assert replay.replayed
        assert replay.review.id == first.review.id
        assert FundingRiskReview.objects.count() == 1
        audit = AuditEvent.objects.get(event_type="funding_risk_review_resolved")
        assert audit.actor_id == analyst.id
        assert audit.organization_id == decision.transfer.agreement.organization_id
        assert audit.payload == {
            "command_id": "funding-review-command-001",
            "decision_id": str(decision.id),
            "outcome": "APPROVED",
            "review_id": str(first.review.id),
            "transfer_id": str(decision.transfer_id),
        }

    def test_competing_resolution_cannot_overwrite_the_first_analyst_decision(self) -> None:
        decision = review_required_decision()
        analyst = risk_analyst()
        resolve_funding_review(
            decision_id=decision.id,
            analyst=analyst,
            outcome=FundingRiskReview.Outcome.REJECTED,
            command_id="funding-review-command-001",
            rationale="A velocidade não foi suficientemente justificada.",
        )

        with self.assertRaises(FundingReviewAlreadyResolved):
            resolve_funding_review(
                decision_id=decision.id,
                analyst=analyst,
                outcome=FundingRiskReview.Outcome.APPROVED,
                command_id="funding-review-command-002",
                rationale="Tentativa concorrente posterior.",
            )

        review = FundingRiskReview.objects.get(decision=decision)
        assert review.outcome == FundingRiskReview.Outcome.REJECTED

    def test_blocked_organization_review_cannot_be_manually_approved(self) -> None:
        decision = review_required_decision()
        organization = decision.transfer.agreement.organization
        organization.risk_blocked = True
        organization.save(update_fields=["risk_blocked"])

        with self.assertRaises(FundingReviewNotPending):
            resolve_funding_review(
                decision_id=decision.id,
                analyst=risk_analyst(),
                outcome=FundingRiskReview.Outcome.APPROVED,
                command_id="funding-review-command-blocked",
                rationale="Não deve ser possível substituir um bloqueio da organização.",
            )

        assert FundingRiskReview.objects.count() == 0

    def test_approved_review_resumes_custody_and_enqueues_one_post_funding_command(self) -> None:
        decision = review_required_decision()
        agreement = decision.transfer.agreement
        agreement.status = EscrowAgreement.Status.REVIEW_REQUIRED
        agreement.save(update_fields=["status"])
        decision.transfer.status = Transfer.Status.PROCESSING
        decision.transfer.save(update_fields=["status"])
        analyst = risk_analyst()

        first = resolve_funding_review_and_enqueue(
            decision_id=decision.id,
            analyst=analyst,
            outcome=FundingRiskReview.Outcome.APPROVED,
            command_id="manual-approve-command-001",
            rationale="A campanha justifica a elevação temporária de volume.",
            correlation_id="manual-approve-correlation-001",
        )
        replay = resolve_funding_review_and_enqueue(
            decision_id=decision.id,
            analyst=analyst,
            outcome=FundingRiskReview.Outcome.APPROVED,
            command_id="manual-approve-command-001",
            rationale="A campanha justifica a elevação temporária de volume.",
            correlation_id="manual-approve-correlation-001",
        )

        agreement.refresh_from_db()
        commands = OutboxEvent.objects.filter(message_type="PostFunding.v1")
        assert not first.replayed
        assert replay.replayed
        assert agreement.status == EscrowAgreement.Status.FUNDING_PROCESSING
        assert commands.count() == 1
        assert commands.get().payload == {
            "agreement_id": str(agreement.id),
            "transfer_id": str(decision.transfer_id),
        }

    def test_rejected_review_enqueues_one_pending_risk_return(self) -> None:
        decision = review_required_decision()
        agreement = decision.transfer.agreement
        agreement.status = EscrowAgreement.Status.REVIEW_REQUIRED
        agreement.save(update_fields=["status"])
        decision.transfer.status = Transfer.Status.PROCESSING
        decision.transfer.save(update_fields=["status"])

        resolution = resolve_funding_review_and_enqueue(
            decision_id=decision.id,
            analyst=risk_analyst(),
            outcome=FundingRiskReview.Outcome.REJECTED,
            command_id="manual-reject-command-001",
            rationale="Não há evidência suficiente para liberar a custódia.",
            correlation_id="manual-reject-correlation-001",
        )

        commands = OutboxEvent.objects.filter(message_type="ReturnRejectedFunding.v1")
        assert not resolution.replayed
        assert commands.count() == 1
        assert commands.get().payload == {
            "agreement_id": str(agreement.id),
            "transfer_id": str(decision.transfer_id),
        }
