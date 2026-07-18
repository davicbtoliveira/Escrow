from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from django.db import transaction
from django.test import TestCase, TransactionTestCase

from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.models import OutboxEvent, ProcessedMessage
from escrow.messaging.outbox import (
    OutboxTransactionRequiredError,
    enqueue_outbox_event,
    process_message_once,
    publish_pending_outbox_events,
)
from escrow.messaging.topology import RISK_FUNDING_QUEUE


def funding_envelope() -> MessageEnvelope:
    return MessageEnvelope.build(
        message_id=UUID("89cc00ba-e41e-4b46-a37e-a3876a4c4981"),
        message_type="EvaluateFundingRisk.v1",
        version=1,
        occurred_at=datetime(2026, 7, 18, 12, 30, tzinfo=UTC),
        correlation_id="correlation-001",
        causation_id="pix-callback-001",
        tenant_id="b90bcdb4-c082-4a6f-8e47-80b5f8a599d7",
        payload={"transfer_id": "2cb79e39-9e0b-420a-85d0-ca765f5272a1"},
    )


class RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[MessageEnvelope, str]] = []

    def publish(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        self.published.append((envelope, routing_key))


class OutboxTests(TestCase):
    def test_committed_event_is_published_only_after_broker_confirmation(self) -> None:
        envelope = funding_envelope()
        with transaction.atomic():
            event = enqueue_outbox_event(envelope, routing_key=RISK_FUNDING_QUEUE.name)

        publisher = RecordingPublisher()
        result = publish_pending_outbox_events(publisher=publisher)
        event.refresh_from_db()

        assert result.published == 1
        assert result.failed == 0
        assert event.published_at is not None
        assert event.publish_attempts == 1
        assert publisher.published == [(envelope, RISK_FUNDING_QUEUE.name)]

    def test_duplicate_message_is_acknowledged_without_repeating_its_effect(self) -> None:
        effect_ids: list[UUID] = []
        envelope = funding_envelope()

        first = process_message_once(
            envelope,
            consumer="ledger.funding",
            effect=lambda: effect_ids.append(envelope.message_id),
        )
        duplicate = process_message_once(
            envelope,
            consumer="ledger.funding",
            effect=lambda: effect_ids.append(envelope.message_id),
        )

        assert first.processed
        assert duplicate.duplicate
        assert effect_ids == [envelope.message_id]
        assert ProcessedMessage.objects.filter(message_id=envelope.message_id).count() == 1

    def test_failed_effect_rolls_back_the_inbox_claim(self) -> None:
        envelope = funding_envelope()

        def failing_effect() -> None:
            raise RuntimeError("database unavailable")

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            process_message_once(
                envelope,
                consumer="ledger.funding",
                effect=failing_effect,
            )

        assert not ProcessedMessage.objects.filter(message_id=envelope.message_id).exists()

    def test_failed_publication_stays_pending_for_a_later_retry(self) -> None:
        class UnavailablePublisher:
            def publish(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
                del envelope, routing_key
                raise RuntimeError("broker unavailable")

        with transaction.atomic():
            event = enqueue_outbox_event(funding_envelope(), routing_key=RISK_FUNDING_QUEUE.name)

        result = publish_pending_outbox_events(publisher=UnavailablePublisher())
        event.refresh_from_db()

        assert result.published == 0
        assert result.failed == 1
        assert event.published_at is None
        assert event.publish_attempts == 1
        assert event.last_error == "RuntimeError"


class OutboxTransactionTests(TransactionTestCase):
    def test_event_must_be_enqueued_inside_the_callers_transaction(self) -> None:
        with self.assertRaises(OutboxTransactionRequiredError):
            enqueue_outbox_event(funding_envelope(), routing_key=RISK_FUNDING_QUEUE.name)

        assert OutboxEvent.objects.count() == 0
