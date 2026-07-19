from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.services import checkout_token_hash
from escrow.audit.models import AuditEvent
from escrow.delivery.services import (
    DeliveryStateConflict,
    enqueue_expired_delivery_refunds,
    report_delivery,
)
from escrow.integrations.models import WebhookEvent
from escrow.integrations.rate_limit import RateLimitDecision
from escrow.ledger.models import LedgerEntry, LedgerTransaction
from escrow.ledger.services import LedgerEntryInput, LedgerPosting, post_ledger_transaction
from escrow.ledger.tasks import refund_funds
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.models import DeadLetterMessage, OutboxEvent
from escrow.organizations.models import Organization
from escrow.payments.models import Transfer

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.get_default_timezone())


def _agreement(
    *,
    status: str = EscrowAgreement.Status.HELD,
    delivery_due_at: datetime | None = None,
    checkout_token: str | None = None,
) -> EscrowAgreement:
    organization = Organization.objects.create(name=f"Expired refund {uuid4().hex}")
    token_hash = (
        checkout_token_hash(checkout_token) if checkout_token is not None else uuid4().hex * 2
    )
    agreement = EscrowAgreement.objects.create(
        organization=organization,
        external_customer_id=f"expired-{uuid4().hex}",
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
        checkout_token_hash=token_hash,
        amount_minor=50_000,
        currency="BRL",
        fee_bps=200,
        delivery_window_days=7,
        funding_confirmed_at=NOW - timedelta(days=7),
        delivery_due_at=delivery_due_at,
        status=status,
        realtime_sequence=2,
    )
    if checkout_token is not None:
        agreement.checkout_token = checkout_token
    return agreement


class ExpiredDeliveryRefundScanTests(TestCase):
    def test_expired_held_agreement_enters_refund_pending_with_exactly_one_command(self) -> None:
        agreement = _agreement(delivery_due_at=NOW - timedelta(seconds=1))

        enqueued = enqueue_expired_delivery_refunds(now=NOW)

        assert enqueued == 1
        agreement.refresh_from_db()
        assert agreement.status == EscrowAgreement.Status.REFUND_PENDING
        transfer = Transfer.objects.get(agreement=agreement, kind=Transfer.Kind.REFUND)
        assert transfer.status == Transfer.Status.PENDING
        assert transfer.amount_minor == agreement.amount_minor
        assert transfer.currency == agreement.currency
        command = OutboxEvent.objects.get(message_type="RefundFunds.v1")
        assert command.routing_key == "ledger.refund"
        assert command.payload == {
            "agreement_id": str(agreement.id),
            "transfer_id": str(transfer.id),
        }
        status_event = OutboxEvent.objects.get(message_type="AgreementStatusChanged.v1")
        assert status_event.payload["status"] == EscrowAgreement.Status.REFUND_PENDING
        webhook_event = WebhookEvent.objects.get(agreement=agreement)
        assert webhook_event.payload["status"] == EscrowAgreement.Status.REFUND_PENDING
        assert webhook_event.payload["refund_reason"] == "DELIVERY_DEADLINE_EXPIRED"
        assert AuditEvent.objects.filter(
            agreement=agreement, event_type="delivery_expired_refund_enqueued"
        ).exists()

    def test_deadline_at_the_scan_instant_is_expired(self) -> None:
        agreement = _agreement(delivery_due_at=NOW)

        enqueued = enqueue_expired_delivery_refunds(now=NOW)

        assert enqueued == 1
        agreement.refresh_from_db()
        assert agreement.status == EscrowAgreement.Status.REFUND_PENDING

    def test_deadline_one_second_in_the_future_is_not_expired(self) -> None:
        agreement = _agreement(delivery_due_at=NOW + timedelta(seconds=1))

        enqueued = enqueue_expired_delivery_refunds(now=NOW)

        assert enqueued == 0
        agreement.refresh_from_db()
        assert agreement.status == EscrowAgreement.Status.HELD
        assert not Transfer.objects.filter(agreement=agreement).exists()

    def test_unfunded_and_terminal_agreements_are_ignored_safely(self) -> None:
        past = NOW - timedelta(days=1)
        ignored = [
            (_agreement(status=s, delivery_due_at=d), s)
            for s, d in [
                (EscrowAgreement.Status.AWAITING_PAYMENT, None),
                (EscrowAgreement.Status.FUNDING_PROCESSING, past),
                (EscrowAgreement.Status.REVIEW_REQUIRED, past),
                (EscrowAgreement.Status.FUNDING_REJECTED, past),
                (EscrowAgreement.Status.INSPECTION, past),
                (EscrowAgreement.Status.RELEASED, past),
                (EscrowAgreement.Status.REFUND_PENDING, past),
                (EscrowAgreement.Status.REFUNDED, past),
                (EscrowAgreement.Status.CANCELLED, past),
                (EscrowAgreement.Status.HELD, None),
            ]
        ]

        enqueued = enqueue_expired_delivery_refunds(now=NOW)

        assert enqueued == 0
        for agreement, expected_status in ignored:
            agreement.refresh_from_db()
            assert agreement.status == expected_status
        assert not Transfer.objects.filter(kind=Transfer.Kind.REFUND).exists()
        assert not OutboxEvent.objects.filter(message_type="RefundFunds.v1").exists()

    def test_repeated_scans_never_duplicate_the_refund(self) -> None:
        agreement = _agreement(delivery_due_at=NOW - timedelta(seconds=1))

        first = enqueue_expired_delivery_refunds(now=NOW)
        second = enqueue_expired_delivery_refunds(now=NOW + timedelta(seconds=30))

        assert (first, second) == (1, 0)
        assert Transfer.objects.filter(agreement=agreement, kind=Transfer.Kind.REFUND).count() == 1
        assert OutboxEvent.objects.filter(message_type="RefundFunds.v1").count() == 1
        assert (
            WebhookEvent.objects.filter(
                agreement=agreement,
                payload__status=EscrowAgreement.Status.REFUND_PENDING,
            ).count()
            == 1
        )

    def test_delivery_report_after_the_refund_is_rejected(self) -> None:
        agreement = _agreement(delivery_due_at=NOW - timedelta(seconds=1))
        enqueue_expired_delivery_refunds(now=NOW)

        with self.assertRaises(DeliveryStateConflict):
            report_delivery(
                organization_id=agreement.organization_id,
                agreement_id=agreement.id,
                idempotency_key="late-delivery-report-001",
                correlation_id="race-correlation-001",
                now=NOW + timedelta(seconds=5),
            )

        assert Transfer.objects.filter(agreement=agreement, kind=Transfer.Kind.REFUND).count() == 1
        assert not Transfer.objects.filter(agreement=agreement, kind=Transfer.Kind.RELEASE).exists()

    def test_reported_delivery_is_never_refunded_by_a_later_scan(self) -> None:
        agreement = _agreement(delivery_due_at=NOW - timedelta(seconds=1))
        result = report_delivery(
            organization_id=agreement.organization_id,
            agreement_id=agreement.id,
            idempotency_key="winning-delivery-report-001",
            correlation_id="race-correlation-002",
            now=NOW - timedelta(seconds=1),
        )
        assert not result.replayed

        enqueued = enqueue_expired_delivery_refunds(now=NOW + timedelta(seconds=5))

        assert enqueued == 0
        agreement.refresh_from_db()
        assert agreement.status == EscrowAgreement.Status.INSPECTION
        assert not Transfer.objects.filter(agreement=agreement, kind=Transfer.Kind.REFUND).exists()


def _envelope_from_outbox(event: OutboxEvent) -> MessageEnvelope:
    return MessageEnvelope.build(
        message_id=event.id,
        message_type=event.message_type,
        version=event.version,
        occurred_at=event.occurred_at,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        tenant_id=event.tenant_id,
        payload=event.payload,
    )


class ExpiredDeliveryRefundPostingTests(TestCase):
    def test_refund_command_posts_balanced_entries_and_refunds_exactly_once(self) -> None:
        agreement = _agreement(delivery_due_at=NOW - timedelta(seconds=1))
        funding = Transfer.objects.create(
            agreement=agreement,
            kind=Transfer.Kind.FUNDING,
            status=Transfer.Status.COMPLETED,
            amount_minor=agreement.amount_minor,
            currency=agreement.currency,
            provider=Transfer.Provider.SANDBOX_PIX,
            provider_reference=f"funding-{agreement.id}",
            idempotency_key=f"funding-{agreement.id}",
        )
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=funding.id,
                kind=LedgerTransaction.Kind.FUNDS_HELD,
                currency=agreement.currency,
                idempotency_key=f"held-{agreement.id}",
                entries=(
                    LedgerEntryInput.debit(
                        "FUNDS_PENDING_RISK", agreement.amount_minor, agreement.currency
                    ),
                    LedgerEntryInput.credit(
                        "ESCROW_LIABILITY", agreement.amount_minor, agreement.currency
                    ),
                ),
            )
        )
        enqueue_expired_delivery_refunds(now=NOW)
        command = OutboxEvent.objects.get(message_type="RefundFunds.v1")
        envelope = _envelope_from_outbox(command)

        refund_funds.apply(args=[envelope.to_dict()]).get()
        refund_funds.apply(args=[envelope.to_dict()]).get()

        agreement.refresh_from_db()
        assert agreement.status == EscrowAgreement.Status.REFUNDED
        refund = Transfer.objects.get(agreement=agreement, kind=Transfer.Kind.REFUND)
        assert refund.status == Transfer.Status.COMPLETED
        refunded = LedgerTransaction.objects.get(kind=LedgerTransaction.Kind.FUNDS_REFUNDED)
        entries = LedgerEntry.objects.filter(ledger_transaction=refunded)
        assert set(entries.values_list("account__code", "debit_minor", "credit_minor")) == {
            ("ESCROW_LIABILITY", 50_000, 0),
            ("PIX_CLEARING", 0, 50_000),
        }
        assert (
            LedgerTransaction.objects.filter(kind=LedgerTransaction.Kind.FUNDS_REFUNDED).count()
            == 1
        )
        status_events = OutboxEvent.objects.filter(
            message_type="AgreementStatusChanged.v1",
        )
        assert status_events.filter(payload__status=EscrowAgreement.Status.REFUNDED).count() == 1

    def _refund_envelope(self, agreement: EscrowAgreement, transfer: Transfer) -> dict[str, object]:
        return MessageEnvelope.build(
            message_id=uuid4(),
            message_type="RefundFunds.v1",
            version=1,
            occurred_at=NOW,
            correlation_id="refund-correlation-001",
            causation_id=str(agreement.id),
            tenant_id=str(agreement.organization_id),
            payload={"agreement_id": str(agreement.id), "transfer_id": str(transfer.id)},
        ).to_dict()

    def test_refund_command_for_an_unready_agreement_is_permanently_rejected(self) -> None:
        agreement = _agreement(delivery_due_at=NOW - timedelta(seconds=1))
        transfer = Transfer.objects.create(
            agreement=agreement,
            kind=Transfer.Kind.REFUND,
            amount_minor=agreement.amount_minor,
            currency=agreement.currency,
            provider=Transfer.Provider.INTERNAL,
            provider_reference=f"premature-refund-{agreement.id}",
            idempotency_key=f"premature-refund:{agreement.id}",
        )

        refund_funds.apply(args=[self._refund_envelope(agreement, transfer)]).get()

        agreement.refresh_from_db()
        transfer.refresh_from_db()
        assert agreement.status == EscrowAgreement.Status.HELD
        assert transfer.status == Transfer.Status.PENDING
        assert not LedgerTransaction.objects.filter(
            kind=LedgerTransaction.Kind.FUNDS_REFUNDED
        ).exists()
        assert DeadLetterMessage.objects.filter(routing_key="ledger.refund").count() == 1

    def test_completed_refund_with_a_foreign_state_is_permanently_rejected(self) -> None:
        agreement = _agreement(delivery_due_at=NOW - timedelta(seconds=1))
        transfer = Transfer.objects.create(
            agreement=agreement,
            kind=Transfer.Kind.REFUND,
            status=Transfer.Status.COMPLETED,
            amount_minor=agreement.amount_minor,
            currency=agreement.currency,
            provider=Transfer.Provider.INTERNAL,
            provider_reference=f"foreign-refund-{agreement.id}",
            idempotency_key=f"foreign-refund:{agreement.id}",
        )

        refund_funds.apply(args=[self._refund_envelope(agreement, transfer)]).get()

        assert DeadLetterMessage.objects.filter(routing_key="ledger.refund").count() == 1

    def test_refund_command_outside_its_tenant_is_permanently_rejected(self) -> None:
        agreement = _agreement(delivery_due_at=NOW - timedelta(seconds=1))
        enqueue_expired_delivery_refunds(now=NOW)
        transfer = Transfer.objects.get(agreement=agreement, kind=Transfer.Kind.REFUND)
        envelope = self._refund_envelope(agreement, transfer)
        envelope["tenant_id"] = str(uuid4())

        refund_funds.apply(args=[envelope]).get()

        agreement.refresh_from_db()
        transfer.refresh_from_db()
        assert agreement.status == EscrowAgreement.Status.REFUND_PENDING
        assert transfer.status == Transfer.Status.PENDING
        assert not LedgerTransaction.objects.filter(
            kind=LedgerTransaction.Kind.FUNDS_REFUNDED
        ).exists()
        assert DeadLetterMessage.objects.filter(routing_key="ledger.refund").count() == 1


class ExpiredDeliveryRefundSurfaceTests(TestCase):
    def _checkout_payload(self, agreement: EscrowAgreement) -> dict[str, object]:
        allowed = RateLimitDecision(allowed=True, retry_after_seconds=0)
        with patch(
            "escrow.agreements.views.check_public_checkout_rate_limit",
            return_value=allowed,
        ):
            response = self.client.get(f"/api/v1/checkout/{agreement.checkout_token}/")
        assert response.status_code == 200
        return response.json()["agreement"]

    def test_checkout_and_webhook_expose_refunded_with_the_deadline_reason(self) -> None:
        agreement = _agreement(
            delivery_due_at=NOW - timedelta(seconds=1),
            checkout_token="chk_expired-refund-surface",
        )
        funding = Transfer.objects.create(
            agreement=agreement,
            kind=Transfer.Kind.FUNDING,
            status=Transfer.Status.COMPLETED,
            amount_minor=agreement.amount_minor,
            currency=agreement.currency,
            provider=Transfer.Provider.SANDBOX_PIX,
            provider_reference=f"surface-funding-{agreement.id}",
            idempotency_key=f"surface-funding-{agreement.id}",
        )
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=funding.id,
                kind=LedgerTransaction.Kind.FUNDS_HELD,
                currency=agreement.currency,
                idempotency_key=f"surface-held-{agreement.id}",
                entries=(
                    LedgerEntryInput.debit(
                        "FUNDS_PENDING_RISK", agreement.amount_minor, agreement.currency
                    ),
                    LedgerEntryInput.credit(
                        "ESCROW_LIABILITY", agreement.amount_minor, agreement.currency
                    ),
                ),
            )
        )

        held_payload = self._checkout_payload(agreement)
        assert held_payload["status"] == EscrowAgreement.Status.HELD
        assert held_payload["refund_reason"] is None

        enqueue_expired_delivery_refunds(now=NOW)
        command = OutboxEvent.objects.get(message_type="RefundFunds.v1")
        refund_funds.apply(args=[_envelope_from_outbox(command).to_dict()]).get()

        refunded_payload = self._checkout_payload(agreement)
        assert refunded_payload["status"] == EscrowAgreement.Status.REFUNDED
        assert refunded_payload["refund_reason"] == "DELIVERY_DEADLINE_EXPIRED"
        webhook_event = WebhookEvent.objects.get(
            agreement=agreement, payload__status=EscrowAgreement.Status.REFUNDED
        )
        assert webhook_event.payload["refund_reason"] == "DELIVERY_DEADLINE_EXPIRED"
