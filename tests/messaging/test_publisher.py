from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import UUID

from django.test import SimpleTestCase, override_settings

from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.publisher import KombuOutboxPublisher, confirming_broker_connection
from escrow.messaging.topology import RISK_FUNDING_QUEUE, task_name_for_routing_key


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


class ConfirmingPublisherTests(SimpleTestCase):
    @override_settings(RABBITMQ_URL="amqp://broker.test/%2F")
    @patch("escrow.messaging.publisher.Connection")
    def test_connection_enables_rabbitmq_publisher_confirms(self, connection: MagicMock) -> None:
        confirming_broker_connection()

        connection.assert_called_once_with(
            "amqp://broker.test/%2F",
            transport_options={"confirm_publish": True},
        )

    @patch("escrow.messaging.publisher.declare_topology")
    @patch("escrow.messaging.publisher.Producer")
    def test_publisher_wraps_the_envelope_in_a_confirmed_celery_task(
        self,
        producer_type: MagicMock,
        declare_topology: MagicMock,
    ) -> None:
        channel = MagicMock()
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.channel.return_value = channel
        producer = producer_type.return_value
        celery_app = MagicMock()
        task_message = object()
        celery_app.amqp.create_task_message.return_value = task_message

        KombuOutboxPublisher(
            connection_factory=lambda: connection,
            celery_app_factory=lambda: celery_app,
        ).publish(
            funding_envelope(),
            routing_key=RISK_FUNDING_QUEUE.name,
        )

        channel.confirm_select.assert_called_once_with()
        declare_topology.assert_called_once_with(channel)
        celery_app.amqp.create_task_message.assert_called_once_with(
            task_id=str(funding_envelope().message_id),
            name=task_name_for_routing_key(RISK_FUNDING_QUEUE.name),
            args=(funding_envelope().to_dict(),),
            kwargs={},
            ignore_result=True,
            root_id="correlation-001",
            parent_id="pix-callback-001",
        )
        celery_app.amqp.send_task_message.assert_called_once_with(
            producer,
            task_name_for_routing_key(RISK_FUNDING_QUEUE.name),
            task_message,
            exchange=RISK_FUNDING_QUEUE.exchange.name,
            queue=RISK_FUNDING_QUEUE,
            serializer="json",
            delivery_mode=2,
            routing_key=RISK_FUNDING_QUEUE.name,
            mandatory=True,
            declare=[RISK_FUNDING_QUEUE],
            retry=True,
            retry_policy={
                "max_retries": 5,
                "interval_start": 0,
                "interval_step": 1,
                "interval_max": 8,
            },
        )
        channel.close.assert_called_once_with()
