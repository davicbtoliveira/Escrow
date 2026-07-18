"""Bounded retry task for publishing durable outbox events."""

from __future__ import annotations

from celery import shared_task  # type: ignore[import-untyped]

from escrow.messaging.outbox import publish_pending_outbox_events
from escrow.messaging.topology import OUTBOX_PUBLISHER_QUEUE


class OutboxPublicationUnavailable(RuntimeError):
    """At least one pending outbox event could not reach RabbitMQ yet."""


@shared_task(  # type: ignore[untyped-decorator]
    name="escrow.messaging.publish_outbox_batch",
    autoretry_for=(OutboxPublicationUnavailable,),
    queue=OUTBOX_PUBLISHER_QUEUE.name,
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
    routing_key=OUTBOX_PUBLISHER_QUEUE.name,
)
def publish_outbox_batch() -> int:
    """Publish a bounded batch; unconfirmed rows remain durable for the next run."""
    result = publish_pending_outbox_events()
    if result.failed:
        raise OutboxPublicationUnavailable("outbox publication remains pending")
    return result.published
