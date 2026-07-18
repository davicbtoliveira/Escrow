from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID

from django.db import transaction
from django.test import TestCase

from escrow.messaging.dlq import capture_dead_letter_message
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.observability import (
    BrokerQueueDepth,
    collect_broker_queue_depths,
    collect_operational_snapshot,
    emit_operational_snapshot,
    emit_operational_snapshot_from_broker,
)
from escrow.messaging.outbox import enqueue_outbox_event
from escrow.messaging.topology import ALL_QUEUES, DEAD_LETTER_QUEUES, RISK_FUNDING_QUEUE


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


class QueueDepthChannel:
    def __init__(self) -> None:
        self.declared: list[str] = []

    def prepare_queue_arguments(
        self, arguments: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        del kwargs
        return arguments

    def exchange_declare(self, **kwargs: object) -> None:
        del kwargs
        return None

    def queue_bind(self, **kwargs: object) -> None:
        del kwargs
        return None

    def queue_declare(self, *, queue: str, **kwargs: object) -> tuple[str, int, int]:
        del kwargs
        self.declared.append(queue)
        return queue, 7 if queue.endswith(".dlq") else 3, 1

    def close(self) -> None:
        return None


class BrokerConnection:
    def __init__(self, channel: QueueDepthChannel) -> None:
        self.channel_value = channel

    def __enter__(self) -> BrokerConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def channel(self) -> QueueDepthChannel:
        return self.channel_value


class MessagingObservabilityTests(TestCase):
    def setUp(self) -> None:
        envelope = funding_envelope()
        with transaction.atomic():
            event = enqueue_outbox_event(envelope, routing_key=RISK_FUNDING_QUEUE.name)
        event.occurred_at = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
        event.publish_attempts = 3
        event.save(update_fields=["occurred_at", "publish_attempts"])
        capture_dead_letter_message(
            body=envelope.to_dict(),
            routing_key=RISK_FUNDING_QUEUE.name,
            error="PermanentMessageError",
            attempt_count=1,
        )

    def test_collects_safe_outbox_and_broker_depth_metrics(self) -> None:
        channel = QueueDepthChannel()
        queue_depths = collect_broker_queue_depths(channel)

        snapshot = collect_operational_snapshot(
            queue_depths=queue_depths,
            now=datetime(2026, 7, 18, 12, 0, tzinfo=UTC) + timedelta(minutes=5),
        )

        assert channel.declared == [queue.name for queue in ALL_QUEUES]
        assert snapshot.pending_outbox_count == 1
        assert snapshot.oldest_pending_outbox_age_seconds == 300
        assert snapshot.maximum_outbox_publish_attempts == 3
        assert snapshot.pending_dead_letter_records == 1
        assert snapshot.dead_letter_depth == 7 * len(DEAD_LETTER_QUEUES)
        assert snapshot.queue_depths == queue_depths

    def test_emits_a_safe_operational_log_without_payload_data(self) -> None:
        queue_depths = (
            BrokerQueueDepth(queue_name="risk.funding", message_count=3, consumer_count=1),
            BrokerQueueDepth(queue_name="risk.funding.dlq", message_count=7, consumer_count=0),
        )

        with self.assertLogs("escrow.messaging", level="INFO") as captured:
            snapshot = emit_operational_snapshot(
                queue_depths=queue_depths,
                now=datetime(2026, 7, 18, 12, 5, tzinfo=UTC),
            )

        assert snapshot.dead_letter_depth == 7
        assert "pending_outbox=1" in captured.output[-1]
        assert "dead_letter_depth=7" in captured.output[-1]
        assert "transfer_id" not in captured.output[-1]

    @patch("escrow.messaging.observability.confirming_broker_connection")
    def test_emits_a_broker_snapshot_after_declaring_only_known_topology(
        self, connection: object
    ) -> None:
        channel = QueueDepthChannel()
        connection.return_value = BrokerConnection(channel)  # type: ignore[attr-defined]

        snapshot = emit_operational_snapshot_from_broker()

        assert snapshot.dead_letter_depth == 7 * len(DEAD_LETTER_QUEUES)
        assert channel.declared == [queue.name for queue in ALL_QUEUES] * 2
