from __future__ import annotations

import json
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.audit.models import AuditEvent
from escrow.ledger.models import LedgerEntry, LedgerTransaction
from escrow.messaging.models import OutboxEvent
from escrow.organizations.models import Organization
from escrow.payments.callbacks import sign_sandbox_callback
from escrow.payments.funding import process_sandbox_pix_callback
from escrow.payments.services import create_sandbox_pix_charge

CALLBACK_SECRET = "sandbox-pix-test-signing-secret"


def agreement() -> EscrowAgreement:
    organization = Organization.objects.create(name="Funding flow organization")
    return EscrowAgreement.objects.create(
        organization=organization,
        external_customer_id="funding-buyer",
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
        checkout_token_hash="c" * 64,
        amount_minor=5_000_000,
        currency="BRL",
        fee_bps=200,
        delivery_window_days=7,
    )


class FundingCallbackFlowTests(TestCase):
    def test_confirmed_callback_posts_pending_risk_once_and_enqueues_risk(self) -> None:
        escrow_agreement = agreement()
        charge = create_sandbox_pix_charge(
            agreement_id=escrow_agreement.id,
            idempotency_key="pix-charge-funding-flow-001",
        ).charge
        escrow_agreement.refresh_from_db()
        assert escrow_agreement.realtime_sequence == 1
        now = timezone.now()
        raw_body = json.dumps(
            {
                "event_id": "sandbox-event-funding-flow-001",
                "provider_reference": charge.provider_reference,
                "outcome": "CONFIRMED",
            },
            separators=(",", ":"),
        ).encode()
        timestamp = str(int(now.timestamp()))
        signature = sign_sandbox_callback(CALLBACK_SECRET, timestamp, raw_body)

        first = process_sandbox_pix_callback(
            raw_body=raw_body,
            timestamp=timestamp,
            signature=signature,
            signing_secret=CALLBACK_SECRET,
            correlation_id="funding-flow-correlation-001",
            now=now,
        )
        replay = process_sandbox_pix_callback(
            raw_body=raw_body,
            timestamp=timestamp,
            signature=signature,
            signing_secret=CALLBACK_SECRET,
            correlation_id="funding-flow-correlation-002",
            now=now,
        )
        escrow_agreement.refresh_from_db()

        assert first.callback.duplicate is False
        assert replay.callback.duplicate is True
        assert first.transfer is not None
        assert first.transfer.status == "PROCESSING"
        assert escrow_agreement.status == EscrowAgreement.Status.FUNDING_PROCESSING
        assert escrow_agreement.funding_confirmed_at == now
        assert escrow_agreement.delivery_due_at == now + timedelta(days=7)
        assert LedgerTransaction.objects.filter(kind="FUNDING_RECEIVED").count() == 1
        assert LedgerEntry.objects.count() == 2
        assert OutboxEvent.objects.filter(message_type="EvaluateFundingRisk.v1").count() == 1
        event = OutboxEvent.objects.get(message_type="EvaluateFundingRisk.v1")
        assert event.message_type == "EvaluateFundingRisk.v1"
        assert event.routing_key == "risk.funding"
        assert event.payload == {
            "agreement_id": str(escrow_agreement.id),
            "transfer_id": str(first.transfer.id),
        }
        status_event = OutboxEvent.objects.get(message_type="AgreementStatusChanged.v1")
        assert status_event.routing_key == "notifications.realtime"
        assert status_event.payload == {
            "agreement_id": str(escrow_agreement.id),
            "status": EscrowAgreement.Status.FUNDING_PROCESSING,
            "sequence": 2,
        }
        assert AuditEvent.objects.filter(event_type="sandbox_pix_callback_confirmed").count() == 1
