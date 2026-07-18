"""RabbitMQ publisher-confirm boundary used by the transactional outbox."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.conf import settings
from kombu import Connection, Producer  # type: ignore[import-untyped]

from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.topology import (
    declare_topology,
    exchange_for_routing_key,
    queue_for_routing_key,
    task_name_for_routing_key,
)


class KombuOutboxPublisher:
    """Publish one envelope as a durable JSON Celery task after broker confirmation."""

    def __init__(
        self,
        connection_factory: Callable[[], Connection] | None = None,
        celery_app_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._connection_factory = connection_factory or confirming_broker_connection
        self._celery_app_factory = celery_app_factory or escrow_celery_app

    def publish(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        exchange = exchange_for_routing_key(routing_key)
        queue = queue_for_routing_key(routing_key)
        task_name = task_name_for_routing_key(routing_key)
        celery_app = self._celery_app_factory()
        task_message = celery_app.amqp.create_task_message(
            task_id=str(envelope.message_id),
            name=task_name,
            args=(envelope.to_dict(),),
            kwargs={},
            ignore_result=True,
            root_id=envelope.correlation_id,
            parent_id=envelope.causation_id,
        )
        with self._connection_factory() as connection:
            channel = connection.channel()
            try:
                channel.confirm_select()
                declare_topology(channel)
                celery_app.amqp.send_task_message(
                    Producer(channel),
                    task_name,
                    task_message,
                    exchange=exchange.name,
                    queue=queue,
                    serializer="json",
                    delivery_mode=2,
                    routing_key=routing_key,
                    mandatory=True,
                    declare=[queue],
                    retry=True,
                    retry_policy={
                        "max_retries": 5,
                        "interval_start": 0,
                        "interval_step": 1,
                        "interval_max": 8,
                    },
                )
            finally:
                channel.close()


def escrow_celery_app() -> Any:
    """Import lazily so Django's app discovery cannot create an import cycle."""
    from escrow.celery import app

    return app


def confirming_broker_connection() -> Connection:
    """Open a broker connection configured for publisher confirms, never pickle."""
    return Connection(
        settings.RABBITMQ_URL,
        transport_options={"confirm_publish": True},
    )
