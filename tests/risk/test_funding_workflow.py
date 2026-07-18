from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.ledger.models import LedgerEntry, LedgerTransaction
from escrow.ledger.services import LedgerEntryInput, LedgerPosting, post_ledger_transaction
from escrow.ledger.tasks import post_funding, refund_funds
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.models import OutboxEvent
from escrow.organizations.models import Organization
from escrow.payments.models import Transfer
from escrow.risk.models import FundingRiskDecision, FundingRiskReview
from escrow.risk.services import (
    RISK_DISPUTE_ANALYST_GROUP,
    evaluate_funding_transfer,
    resolve_funding_review_and_enqueue,
)
from escrow.risk.tasks import evaluate_funding_risk


def _processing_transfer(*, blocked: bool = False, amount_minor: int = 5_000_000) -> Transfer:
    organization = Organization.objects.create(name="Funding workflow", risk_blocked=blocked)
    now = timezone.now()
    Organization.objects.filter(id=organization.id).update(created_at=now - timedelta(days=6))
    agreement = EscrowAgreement.objects.create(
        organization=organization,
        external_customer_id=f"workflow-{uuid4().hex}",
        customer_name_masked="A***",
        customer_email_masked="a***@example.test",
        customer_document_masked="***.***.***-25",
        customer_document_kind="CPF",
        customer_email_blind_index=uuid4().hex * 2,
        customer_document_blind_index=uuid4().hex * 2,
        customer_pii_ciphertext=b"ciphertext",
        customer_pii_nonce=b"nonce",
        customer_pii_encrypted_data_key=b"encrypted-key",
        customer_pii_kms_key_id="test-key",
        checkout_token_hash=uuid4().hex * 2,
        amount_minor=amount_minor,
        currency="BRL",
        fee_bps=200,
        delivery_window_days=7,
        status=EscrowAgreement.Status.FUNDING_PROCESSING,
        realtime_sequence=2,
    )
    return Transfer.objects.create(
        agreement=agreement,
        kind=Transfer.Kind.FUNDING,
        status=Transfer.Status.PROCESSING,
        amount_minor=amount_minor,
        currency="BRL",
        provider=Transfer.Provider.SANDBOX_PIX,
        provider_reference=f"funding-workflow-{agreement.id}",
        idempotency_key=f"funding-workflow-{agreement.id}",
    )


def _risk_command(transfer: Transfer) -> dict[str, object]:
    return MessageEnvelope.build(
        message_id=uuid4(),
        message_type="EvaluateFundingRisk.v1",
        version=1,
        occurred_at=timezone.now(),
        correlation_id="funding-workflow-correlation-001",
        causation_id="sandbox-callback-001",
        tenant_id=str(transfer.agreement.organization_id),
        payload={"agreement_id": str(transfer.agreement_id), "transfer_id": str(transfer.id)},
    ).to_dict()


class FundingRiskWorkflowTests(TestCase):
    def test_review_required_updates_safe_customer_status_without_posting_custody(self) -> None:
        transfer = _processing_transfer()

        evaluate_funding_risk.apply(args=[_risk_command(transfer)]).get()

        transfer.agreement.refresh_from_db()
        decision = FundingRiskDecision.objects.get(transfer=transfer)
        assert decision.outcome == "REVIEW_REQUIRED"
        assert transfer.agreement.status == EscrowAgreement.Status.REVIEW_REQUIRED
        assert OutboxEvent.objects.filter(message_type="PostFunding.v1").count() == 0
        status_event = OutboxEvent.objects.get(message_type="AgreementStatusChanged.v1")
        assert status_event.payload["status"] == EscrowAgreement.Status.REVIEW_REQUIRED

    def test_blocked_organization_enqueues_exactly_one_automatic_return(self) -> None:
        transfer = _processing_transfer(blocked=True, amount_minor=50_000)
        command = _risk_command(transfer)

        evaluate_funding_risk.apply(args=[command]).get()
        evaluate_funding_risk.apply(args=[command]).get()

        decision = FundingRiskDecision.objects.get(transfer=transfer)
        returns = OutboxEvent.objects.filter(message_type="ReturnRejectedFunding.v1")
        assert decision.outcome == "REJECTED"
        assert returns.count() == 1
        assert returns.get().payload == {
            "agreement_id": str(transfer.agreement_id),
            "transfer_id": str(transfer.id),
        }

    def test_rejected_funding_returns_pending_risk_value_to_pix_clearing_once(self) -> None:
        transfer = _processing_transfer(blocked=True, amount_minor=50_000)
        evaluate_funding_transfer(transfer.id, now=timezone.now())
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=transfer.id,
                kind=LedgerTransaction.Kind.FUNDING_RECEIVED,
                currency=transfer.currency,
                idempotency_key=f"received-before-return:{transfer.id}",
                entries=(
                    LedgerEntryInput.debit(
                        "PIX_CLEARING", transfer.amount_minor, transfer.currency
                    ),
                    LedgerEntryInput.credit(
                        "FUNDS_PENDING_RISK", transfer.amount_minor, transfer.currency
                    ),
                ),
            )
        )
        envelope = MessageEnvelope.build(
            message_id=uuid4(),
            message_type="ReturnRejectedFunding.v1",
            version=1,
            occurred_at=timezone.now(),
            correlation_id="funding-return-correlation-001",
            causation_id="risk-decision-001",
            tenant_id=str(transfer.agreement.organization_id),
            payload={"agreement_id": str(transfer.agreement_id), "transfer_id": str(transfer.id)},
        )

        refund_funds.apply(args=[envelope.to_dict()]).get()
        refund_funds.apply(args=[envelope.to_dict()]).get()

        transfer.refresh_from_db()
        transfer.agreement.refresh_from_db()
        returned = LedgerTransaction.objects.get(kind=LedgerTransaction.Kind.FUNDING_REJECTED)
        entries = LedgerEntry.objects.filter(ledger_transaction=returned)
        assert transfer.status == Transfer.Status.FAILED
        assert transfer.agreement.status == EscrowAgreement.Status.FUNDING_REJECTED
        assert set(entries.values_list("account__code", "debit_minor", "credit_minor")) == {
            ("FUNDS_PENDING_RISK", 50_000, 0),
            ("PIX_CLEARING", 0, 50_000),
        }
        assert (
            LedgerTransaction.objects.filter(kind=LedgerTransaction.Kind.FUNDING_REJECTED).count()
            == 1
        )

    def test_manual_rejection_uses_the_same_once_only_return_posting(self) -> None:
        transfer = _processing_transfer()
        decision = evaluate_funding_transfer(transfer.id, now=timezone.now())
        assert decision.outcome == "REVIEW_REQUIRED"
        transfer.agreement.status = EscrowAgreement.Status.REVIEW_REQUIRED
        transfer.agreement.save(update_fields=["status"])
        analyst = get_user_model().objects.create_user(
            email="manual-return@risk.test",
            password="Uma senha forte e exclusiva 2026!",
            is_staff=True,
        )
        analyst_group, _ = Group.objects.get_or_create(name=RISK_DISPUTE_ANALYST_GROUP)
        analyst.groups.add(analyst_group)
        resolution = resolve_funding_review_and_enqueue(
            decision_id=decision.id,
            analyst=analyst,
            outcome=FundingRiskReview.Outcome.REJECTED,
            command_id="manual-return-command-001",
            rationale="O padrão de risco não foi justificado.",
            correlation_id="manual-return-correlation-001",
        )
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=transfer.id,
                kind=LedgerTransaction.Kind.FUNDING_RECEIVED,
                currency=transfer.currency,
                idempotency_key=f"received-before-manual-return:{transfer.id}",
                entries=(
                    LedgerEntryInput.debit(
                        "PIX_CLEARING", transfer.amount_minor, transfer.currency
                    ),
                    LedgerEntryInput.credit(
                        "FUNDS_PENDING_RISK", transfer.amount_minor, transfer.currency
                    ),
                ),
            )
        )
        event = OutboxEvent.objects.get(
            message_type="ReturnRejectedFunding.v1",
            payload={"agreement_id": str(transfer.agreement_id), "transfer_id": str(transfer.id)},
        )
        envelope = MessageEnvelope.build(
            message_id=event.id,
            message_type=event.message_type,
            version=event.version,
            occurred_at=event.occurred_at,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            tenant_id=event.tenant_id,
            payload=event.payload,
        )

        refund_funds.apply(args=[envelope.to_dict()]).get()

        transfer.refresh_from_db()
        transfer.agreement.refresh_from_db()
        assert not resolution.replayed
        assert transfer.status == Transfer.Status.FAILED
        assert transfer.agreement.status == EscrowAgreement.Status.FUNDING_REJECTED

    def test_manual_approval_uses_the_same_once_only_custody_posting(self) -> None:
        transfer = _processing_transfer()
        decision = evaluate_funding_transfer(transfer.id, now=timezone.now())
        assert decision.outcome == "REVIEW_REQUIRED"
        transfer.agreement.status = EscrowAgreement.Status.REVIEW_REQUIRED
        transfer.agreement.save(update_fields=["status"])
        analyst = get_user_model().objects.create_user(
            email="manual-approval@risk.test",
            password="Uma senha forte e exclusiva 2026!",
            is_staff=True,
        )
        analyst_group, _ = Group.objects.get_or_create(name=RISK_DISPUTE_ANALYST_GROUP)
        analyst.groups.add(analyst_group)
        resolve_funding_review_and_enqueue(
            decision_id=decision.id,
            analyst=analyst,
            outcome=FundingRiskReview.Outcome.APPROVED,
            command_id="manual-approval-command-001",
            rationale="O pico é compatível com a campanha cadastrada.",
            correlation_id="manual-approval-correlation-001",
        )
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=transfer.id,
                kind=LedgerTransaction.Kind.FUNDING_RECEIVED,
                currency=transfer.currency,
                idempotency_key=f"received-before-manual-approval:{transfer.id}",
                entries=(
                    LedgerEntryInput.debit(
                        "PIX_CLEARING", transfer.amount_minor, transfer.currency
                    ),
                    LedgerEntryInput.credit(
                        "FUNDS_PENDING_RISK", transfer.amount_minor, transfer.currency
                    ),
                ),
            )
        )
        event = OutboxEvent.objects.get(
            message_type="PostFunding.v1",
            payload={"agreement_id": str(transfer.agreement_id), "transfer_id": str(transfer.id)},
        )
        envelope = MessageEnvelope.build(
            message_id=event.id,
            message_type=event.message_type,
            version=event.version,
            occurred_at=event.occurred_at,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            tenant_id=event.tenant_id,
            payload=event.payload,
        )

        post_funding.apply(args=[envelope.to_dict()]).get()

        transfer.refresh_from_db()
        transfer.agreement.refresh_from_db()
        assert transfer.status == Transfer.Status.COMPLETED
        assert transfer.agreement.status == EscrowAgreement.Status.HELD
