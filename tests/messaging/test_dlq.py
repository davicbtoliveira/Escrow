from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock, patch
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase

from escrow.audit.models import AuditEvent
from escrow.messaging.dlq import (
    DeadLetterReplayError,
    KombuDeadLetterTransport,
    capture_dead_letter_message,
    replay_dead_letter_message,
)
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.models import DeadLetterMessage


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


class RecordingTransport:
    def __init__(self) -> None:
        self.replays: list[tuple[DeadLetterMessage, MessageEnvelope]] = []

    def replay(self, dead_letter: DeadLetterMessage, envelope: MessageEnvelope) -> None:
        self.replays.append((dead_letter, envelope))


class FailingTransport:
    def replay(self, dead_letter: DeadLetterMessage, envelope: MessageEnvelope) -> None:
        del dead_letter, envelope
        raise RuntimeError("broker unavailable")


class RecordingPublisher:
    def __init__(self) -> None:
        self.publications: list[tuple[MessageEnvelope, str]] = []

    def publish(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        self.publications.append((envelope, routing_key))


class BrokerMessage:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.ack = Mock()
        self.reject = Mock()


class DeadLetterQueue:
    def __init__(self, message: BrokerMessage | None) -> None:
        self.message = message

    def __call__(self, channel: object) -> DeadLetterQueue:
        del channel
        return self

    def get(self, *, no_ack: bool) -> BrokerMessage | None:
        assert not no_ack
        return self.message


class BrokerConnection:
    def __init__(self) -> None:
        self.channel_value = Mock()

    def __enter__(self) -> BrokerConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def channel(self) -> Mock:
        return self.channel_value


class DeadLetterReplayTests(TestCase):
    def setUp(self) -> None:
        self.actor = get_user_model().objects.create_user(
            email="platform-admin@escrow.example",
            password="Uma senha forte e exclusiva 2026!",
            is_staff=True,
        )
        self.dead_letter = capture_dead_letter_message(
            body=funding_envelope().to_dict(),
            routing_key="risk.funding",
            error="PermanentMessageError",
            attempt_count=1,
            source_task_id=str(funding_envelope().message_id),
            headers={"traceparent": "00-portfolio-trace-01"},
        )

    def test_staff_replays_one_selected_message_with_its_original_identity_and_audit(self) -> None:
        transport = RecordingTransport()

        result = replay_dead_letter_message(
            self.dead_letter.id,
            actor=self.actor,
            transport=transport,
        )

        self.dead_letter.refresh_from_db()
        assert result.dead_letter_id == self.dead_letter.id
        assert result.original_message_id == funding_envelope().message_id
        assert transport.replays == [(self.dead_letter, funding_envelope())]
        assert self.dead_letter.replayed_at is not None
        assert self.dead_letter.replay_attempts == 1
        audit = AuditEvent.objects.get(event_type="dead_letter_replayed")
        assert audit.actor == self.actor
        assert audit.correlation_id == "correlation-001"
        assert audit.payload == {
            "dead_letter_id": str(self.dead_letter.id),
            "message_id": str(funding_envelope().message_id),
            "routing_key": "risk.funding",
            "replay_attempt": 1,
        }

    def test_replay_is_never_automatic_or_repeated_for_the_same_dlq_record(self) -> None:
        transport = RecordingTransport()
        replay_dead_letter_message(
            self.dead_letter.id,
            actor=self.actor,
            transport=transport,
        )

        with self.assertRaisesRegex(DeadLetterReplayError, "already replayed"):
            replay_dead_letter_message(
                self.dead_letter.id,
                actor=self.actor,
                transport=transport,
            )

        assert len(transport.replays) == 1

    def test_failed_replay_keeps_the_record_pending_and_records_an_audit_outcome(self) -> None:
        with self.assertRaisesRegex(DeadLetterReplayError, "could not be published"):
            replay_dead_letter_message(
                self.dead_letter.id,
                actor=self.actor,
                transport=FailingTransport(),
            )

        self.dead_letter.refresh_from_db()
        assert self.dead_letter.replayed_at is None
        assert self.dead_letter.replay_attempts == 1
        assert self.dead_letter.last_replay_error == "RuntimeError"
        audit = AuditEvent.objects.get(event_type="dead_letter_replay_failed")
        assert audit.actor == self.actor

    def test_non_staff_user_cannot_replay_a_dlq_message(self) -> None:
        non_staff = get_user_model().objects.create_user(
            email="operator@escrow.example",
            password="Uma senha forte e exclusiva 2026!",
        )

        with self.assertRaises(PermissionDenied):
            replay_dead_letter_message(
                self.dead_letter.id,
                actor=non_staff,
                transport=RecordingTransport(),
            )

        assert not AuditEvent.objects.exists()

    @patch("escrow.messaging.dlq.declare_topology")
    @patch("escrow.messaging.dlq.dead_letter_queue_for_routing_key")
    @patch("escrow.messaging.dlq.confirming_broker_connection")
    def test_broker_transport_republishes_the_matching_dlq_head_then_acknowledges_it(
        self,
        connection: Mock,
        queue_for_route: Mock,
        declare_topology: Mock,
    ) -> None:
        message = BrokerMessage(((funding_envelope().to_dict(),), {}, {}))
        queue = DeadLetterQueue(message)
        broker_connection = BrokerConnection()
        connection.return_value = broker_connection
        queue_for_route.return_value = queue
        publisher = RecordingPublisher()

        KombuDeadLetterTransport(publisher=publisher).replay(
            self.dead_letter,
            funding_envelope(),
        )

        assert publisher.publications == [(funding_envelope(), "risk.funding")]
        message.ack.assert_called_once_with()
        message.reject.assert_not_called()
        declare_topology.assert_called_once_with(broker_connection.channel_value)
        broker_connection.channel_value.close.assert_called_once_with()

    @patch("escrow.messaging.dlq.declare_topology")
    @patch("escrow.messaging.dlq.dead_letter_queue_for_routing_key")
    @patch("escrow.messaging.dlq.confirming_broker_connection")
    def test_broker_transport_requeues_an_unselected_dlq_head_without_publishing_it(
        self,
        connection: Mock,
        queue_for_route: Mock,
        _: Mock,
    ) -> None:
        other = MessageEnvelope.build(
            message_id=UUID("c94f7ce1-ea06-4c59-b813-b0c705dfb62d"),
            message_type="EvaluateFundingRisk.v1",
            version=1,
            occurred_at=datetime(2026, 7, 18, 12, 30, tzinfo=UTC),
            correlation_id="correlation-002",
            causation_id=None,
            tenant_id="b90bcdb4-c082-4a6f-8e47-80b5f8a599d7",
            payload={"transfer_id": "2cb79e39-9e0b-420a-85d0-ca765f5272a1"},
        )
        message = BrokerMessage(((other.to_dict(),), {}, {}))
        queue_for_route.return_value = DeadLetterQueue(message)
        connection.return_value = BrokerConnection()
        publisher = RecordingPublisher()

        with self.assertRaisesRegex(DeadLetterReplayError, "not at the DLQ head"):
            KombuDeadLetterTransport(publisher=publisher).replay(
                self.dead_letter,
                funding_envelope(),
            )

        assert not publisher.publications
        message.ack.assert_not_called()
        message.reject.assert_called_once_with(requeue=True)
