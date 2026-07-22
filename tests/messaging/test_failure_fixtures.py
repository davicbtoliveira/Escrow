from __future__ import annotations

import json
from datetime import UTC, datetime
from io import StringIO
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from celery.exceptions import Reject
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.audit.models import AuditEvent
from escrow.ledger.models import LedgerTransaction
from escrow.ledger.services import LedgerEntryInput, LedgerPosting, post_ledger_transaction
from escrow.ledger.tasks import post_funding
from escrow.messaging.consumer import consume_envelope_task, consume_message_once
from escrow.messaging.dlq import capture_dead_letter_message
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.models import DeadLetterMessage, OutboxEvent, ProcessedMessage
from escrow.messaging.outbox import (
    enqueue_outbox_event,
    process_message_once,
    publish_pending_outbox_events,
)
from escrow.messaging.topology import LEDGER_FUNDING_QUEUE, RISK_FUNDING_QUEUE
from escrow.organizations.models import Organization
from escrow.payments.models import Transfer
from escrow.risk.models import FundingRiskDecision, FundingRiskPolicy
from escrow.risk.policy import FundingRiskOutcome


def _sample_agreement() -> EscrowAgreement:
    org = Organization.objects.create(name="Sandbox Failure Test Org")
    return EscrowAgreement.objects.create(
        organization=org,
        external_customer_id="cust-failure-001",
        customer_name_masked="F***",
        customer_email_masked="f***@example.test",
        customer_document_masked="***.***.***-00",
        customer_document_kind="CPF",
        customer_email_blind_index="c" * 64,
        customer_document_blind_index="d" * 64,
        customer_pii_ciphertext=b"cipher",
        customer_pii_nonce=b"nonce",
        customer_pii_encrypted_data_key=b"key",
        customer_pii_kms_key_id="key-id",
        checkout_token_hash=f"hash-{uuid4().hex}",
        amount_minor=100_000,
        currency="BRL",
        fee_bps=200,
        delivery_window_days=7,
    )


def _build_envelope(
    message_type: str,
    payload: dict[str, object],
    tenant_id: str = "tenant-sandbox-001",
    message_id: UUID | None = None,
    correlation_id: str = "corr-sandbox-001",
    causation_id: str = "caus-sandbox-001",
) -> MessageEnvelope:
    return MessageEnvelope.build(
        message_id=message_id or uuid4(),
        message_type=message_type,
        version=1,
        occurred_at=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        correlation_id=correlation_id,
        causation_id=causation_id,
        tenant_id=tenant_id,
        payload=payload,
    )


class SimulatedTask:
    def __init__(self, retries: int = 0, task_id: str = "") -> None:
        self.request = Mock()
        self.request.retries = retries
        self.request.id = task_id or str(uuid4())
        self.request.headers = {"traceparent": "00-test-trace-id"}
        self.retried_excs: list[BaseException] = []

    def retry(self, *, exc: BaseException, countdown: float) -> None:
        del countdown
        self.retried_excs.append(exc)
        raise exc


class SandboxFailureAndRecoveryTests(TestCase):
    def setUp(self) -> None:
        self.staff_user = get_user_model().objects.create_user(
            email="ops-admin@escrow.example",
            password="StrongPassword2026!",
            is_staff=True,
        )

    def test_database_outage_recovery_resumes_outbox_publication_and_task_execution(self) -> None:
        """Scenario 1: Outbox publish failure keeps event pending; recovery publishes event cleanly."""
        agreement = _sample_agreement()
        envelope = _build_envelope(
            "EvaluateFundingRisk.v1",
            {"transfer_id": str(uuid4()), "agreement_id": str(agreement.id)},
            tenant_id=str(agreement.organization_id),
        )

        with transaction.atomic():
            event = enqueue_outbox_event(envelope, routing_key=RISK_FUNDING_QUEUE.name)

        # 1. Transport/DB Error during outbox publish
        failing_publisher = Mock()
        failing_publisher.publish.side_effect = RuntimeError("DB connection dropped")

        result1 = publish_pending_outbox_events(publisher=failing_publisher)
        assert result1.published == 0
        assert result1.failed == 1
        event.refresh_from_db()
        assert event.published_at is None
        assert event.publish_attempts == 1

        # 2. DB / Service Restored -> Outbox publishes event
        recording_publisher = Mock()
        result2 = publish_pending_outbox_events(publisher=recording_publisher)
        assert result2.published == 1
        assert result2.failed == 0
        event.refresh_from_db()
        assert event.published_at is not None
        recording_publisher.publish.assert_called_once()

    def test_broker_outage_recovery_republishes_all_committed_outbox_events(self) -> None:
        """Scenario 2: Broker outage holds outbox events pending; recovery publishes without dropping messages."""
        envelope1 = _build_envelope("PostFunding.v1", {"amount": 5000})
        envelope2 = _build_envelope("PostFunding.v1", {"amount": 10000})

        with transaction.atomic():
            enqueue_outbox_event(envelope1, routing_key=LEDGER_FUNDING_QUEUE.name)
            enqueue_outbox_event(envelope2, routing_key=LEDGER_FUNDING_QUEUE.name)

        # Broker down
        broker_down = Mock()
        broker_down.publish.side_effect = RuntimeError("RabbitMQ connection refused")
        res1 = publish_pending_outbox_events(publisher=broker_down)
        assert res1.published == 0
        assert res1.failed == 1

        # Broker back online
        recorded_pub = Mock()
        res2 = publish_pending_outbox_events(publisher=recorded_pub)
        assert res2.published == 2
        assert res2.failed == 0
        assert recorded_pub.publish.call_count == 2

    def test_worker_death_redelivery_is_deduplicated_by_inbox(self) -> None:
        """Scenario 3: Worker dies mid-processing / redelivers task. Inbox ensures effect happens exactly once."""
        envelope = _build_envelope("EvaluateFundingRisk.v1", {"transfer_id": str(uuid4())})
        effect_counter = Mock()

        # First run (Worker executes effect & commits processed message)
        res1 = process_message_once(
            envelope,
            consumer="risk.funding",
            effect=effect_counter,
        )
        assert res1.processed
        assert not res1.duplicate
        assert effect_counter.call_count == 1

        # Worker crashed before ACK -> Broker redelivers same task envelope
        res2 = process_message_once(
            envelope,
            consumer="risk.funding",
            effect=effect_counter,
        )
        assert not res2.processed
        assert res2.duplicate
        assert effect_counter.call_count == 1
        assert ProcessedMessage.objects.filter(message_id=envelope.message_id).count() == 1

    def test_duplicate_delivery_on_ledger_guarantees_single_posting_invariance(self) -> None:
        """Scenario 4: Duplicate delivery on ledger funding queue results in exactly one set of ledger entries."""
        agreement = _sample_agreement()
        agreement.status = EscrowAgreement.Status.FUNDING_PROCESSING
        agreement.save(update_fields=["status"])
        transfer = Transfer.objects.create(
            agreement=agreement,
            provider_reference="pix-ref-sandbox-001",
            amount_minor=100_000,
            currency="BRL",
            kind=Transfer.Kind.FUNDING,
            status=Transfer.Status.PROCESSING,
        )
        policy = FundingRiskPolicy.objects.create(version="1.0", configuration={})
        FundingRiskDecision.objects.create(
            transfer=transfer,
            policy=policy,
            policy_version="1.0",
            policy_configuration={},
            outcome=FundingRiskOutcome.APPROVED,
            score=0,
            reasons=[],
            inputs={},
            evaluated_at=timezone.now(),
        )

        post_ledger_transaction(
            LedgerPosting(
                transfer_id=transfer.id,
                kind=LedgerTransaction.Kind.FUNDING_RECEIVED,
                currency=transfer.currency,
                idempotency_key=f"funding-received:{transfer.id}",
                entries=(
                    LedgerEntryInput.debit("PIX_CLEARING", 100_000, "BRL"),
                    LedgerEntryInput.credit("FUNDS_PENDING_RISK", 100_000, "BRL"),
                ),
            )
        )

        envelope = _build_envelope(
            "PostFunding.v1",
            {
                "transfer_id": str(transfer.id),
                "agreement_id": str(agreement.id),
            },
            tenant_id=str(agreement.organization_id),
        )
        body = envelope.to_dict()

        # Run task 1
        post_funding.apply(args=[body]).get()
        # Run task 2 (duplicate delivery)
        post_funding.apply(args=[body]).get()

        # Assert ledger transactions: exactly 1 FUNDING_RECEIVED, exactly 1 FUNDS_HELD
        funding_txs = LedgerTransaction.objects.filter(
            kind=LedgerTransaction.Kind.FUNDING_RECEIVED
        )
        held_txs = LedgerTransaction.objects.filter(kind=LedgerTransaction.Kind.FUNDS_HELD)

        assert funding_txs.count() == 1
        assert held_txs.count() == 1
        assert ProcessedMessage.objects.filter(message_id=envelope.message_id).count() == 1

        agreement.refresh_from_db()
        assert agreement.status == EscrowAgreement.Status.HELD

    def test_malformed_payload_is_rejected_immediately_to_dlq(self) -> None:
        """Scenario 5: Malformed payload / invalid envelope schema dead-letters immediately without retry loops or state changes."""
        malformed_body = {
            "message_id": str(uuid4()),
            "type": "EvaluateFundingRisk.v1",
        }

        task = SimulatedTask(retries=0)

        with self.assertRaises(Reject) as raised:
            consume_envelope_task(
                task,
                malformed_body,
                expected_type="EvaluateFundingRisk.v1",
                expected_version=1,
                consumer="risk.funding",
                effect=Mock(),
            )

        assert not raised.exception.requeue
        assert raised.exception.reason == "invalid_envelope"

    def test_dlq_poison_message_preserves_metadata_and_replays_via_audited_command(self) -> None:
        """Scenario 6: Poison message captured in DLQ with metadata -> Operator runs replay_dead_letter -> Effect succeeds & audit recorded."""
        agreement = _sample_agreement()
        envelope = _build_envelope(
            "EvaluateFundingRisk.v1",
            {"transfer_id": str(uuid4()), "agreement_id": str(agreement.id)},
            tenant_id=str(agreement.organization_id),
        )

        # Capture poison message in DLQ
        dead_letter = capture_dead_letter_message(
            body=envelope.to_dict(),
            routing_key="risk.funding",
            error="PermanentMessageError",
            attempt_count=1,
            source_task_id="task-poison-001",
            headers={"traceparent": "00-poison-trace-id"},
        )

        assert dead_letter.original_message_id == envelope.message_id
        assert dead_letter.routing_key == "risk.funding"
        assert dead_letter.headers == {"traceparent": "00-poison-trace-id"}
        assert dead_letter.replayed_at is None

        # Execute audited replay command as staff user with mock transport
        mock_transport = Mock()
        with patch("escrow.messaging.dlq.KombuDeadLetterTransport", return_value=mock_transport):
            out = StringIO()
            call_command(
                "replay_dead_letter",
                str(dead_letter.id),
                "--actor-email",
                self.staff_user.email,
                stdout=out,
            )

        dead_letter.refresh_from_db()
        assert dead_letter.replayed_at is not None
        assert dead_letter.replay_attempts == 1

        # Verify audit trail
        audit = AuditEvent.objects.get(event_type="dead_letter_replayed")
        assert audit.actor == self.staff_user
        assert audit.correlation_id == envelope.correlation_id
        assert audit.payload["dead_letter_id"] == str(dead_letter.id)
        assert audit.payload["message_id"] == str(envelope.message_id)
