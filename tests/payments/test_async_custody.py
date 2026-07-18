from __future__ import annotations

import json
from uuid import uuid4

from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.ledger.models import LedgerEntry, LedgerTransaction
from escrow.ledger.tasks import post_funding
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.models import OutboxEvent, ProcessedMessage
from escrow.organizations.models import Organization
from escrow.payments.callbacks import sign_sandbox_callback
from escrow.payments.funding import process_sandbox_pix_callback
from escrow.payments.models import Transfer
from escrow.payments.services import create_sandbox_pix_charge
from escrow.risk.models import FundingRiskDecision
from escrow.risk.tasks import evaluate_funding_risk

_CALLBACK_SECRET = "sandbox-pix-test-signing-secret"


def _agreement() -> EscrowAgreement:
    organization = Organization.objects.create(name="Async custody organization")
    return EscrowAgreement.objects.create(
        organization=organization,
        external_customer_id="async-custody-buyer",
        customer_name_masked="A***",
        customer_email_masked="a***@example.test",
        customer_document_masked="***.***.***-25",
        customer_document_kind="CPF",
        customer_email_blind_index="a" * 64,
        customer_document_blind_index="b" * 64,
        customer_pii_ciphertext=b"ciphertext",
        customer_pii_nonce=b"nonce",
        customer_pii_encrypted_data_key=b"encrypted-key",
        customer_pii_kms_key_id="test-key",
        checkout_token_hash=f"checkout-{uuid4().hex}",
        amount_minor=50_000,
        currency="BRL",
        fee_bps=200,
        delivery_window_days=7,
    )


def _envelope(event: OutboxEvent) -> dict[str, object]:
    return MessageEnvelope.build(
        message_id=event.id,
        message_type=event.message_type,
        version=event.version,
        occurred_at=event.occurred_at,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        tenant_id=event.tenant_id,
        payload=event.payload,
    ).to_dict()


class AsyncCustodyFlowTests(TestCase):
    def test_confirmed_pix_reaches_held_escrow_once_despite_replayed_messages(self) -> None:
        agreement = _agreement()
        charge = create_sandbox_pix_charge(
            agreement_id=agreement.id,
            idempotency_key="async-custody-pix-charge-001",
        ).charge
        now = timezone.now()
        raw_body = json.dumps(
            {
                "event_id": "async-custody-callback-001",
                "provider_reference": charge.provider_reference,
                "outcome": "CONFIRMED",
            },
            separators=(",", ":"),
        ).encode()
        timestamp = str(int(now.timestamp()))
        callback = process_sandbox_pix_callback(
            raw_body=raw_body,
            timestamp=timestamp,
            signature=sign_sandbox_callback(_CALLBACK_SECRET, timestamp, raw_body),
            signing_secret=_CALLBACK_SECRET,
            correlation_id="async-custody-correlation-001",
            now=now,
        )
        assert callback.transfer is not None

        risk_event = OutboxEvent.objects.get(message_type="EvaluateFundingRisk.v1")
        evaluate_funding_risk.apply(args=[_envelope(risk_event)]).get()
        evaluate_funding_risk.apply(args=[_envelope(risk_event)]).get()

        decision = FundingRiskDecision.objects.get(transfer=callback.transfer)
        assert decision.outcome == "APPROVED"
        assert ProcessedMessage.objects.filter(message_id=risk_event.id).count() == 1
        ledger_events = OutboxEvent.objects.filter(message_type="PostFunding.v1")
        assert ledger_events.count() == 1

        ledger_event = ledger_events.get()
        post_funding.apply(args=[_envelope(ledger_event)]).get()
        post_funding.apply(args=[_envelope(ledger_event)]).get()

        agreement.refresh_from_db()
        transfer = Transfer.objects.get(id=callback.transfer.id)
        held_posting = LedgerTransaction.objects.get(kind=LedgerTransaction.Kind.FUNDS_HELD)
        held_entries = LedgerEntry.objects.filter(ledger_transaction=held_posting)
        assert agreement.status == EscrowAgreement.Status.HELD
        assert transfer.status == Transfer.Status.COMPLETED
        assert ProcessedMessage.objects.filter(message_id=ledger_event.id).count() == 1
        assert (
            LedgerTransaction.objects.filter(kind=LedgerTransaction.Kind.FUNDING_RECEIVED).count()
            == 1
        )
        assert LedgerTransaction.objects.filter(kind=LedgerTransaction.Kind.FUNDS_HELD).count() == 1
        assert OutboxEvent.objects.filter(message_type="AgreementStatusChanged.v1").count() == 2
        assert set(held_entries.values_list("account__code", "debit_minor", "credit_minor")) == {
            ("FUNDS_PENDING_RISK", 50_000, 0),
            ("ESCROW_LIABILITY", 0, 50_000),
        }
