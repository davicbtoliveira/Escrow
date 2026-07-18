"""Safe, low-cardinality operational snapshots for asynchronous processing."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db.models import Max, Min
from django.utils import timezone

from escrow.messaging.models import DeadLetterMessage, OutboxEvent
from escrow.messaging.publisher import confirming_broker_connection
from escrow.messaging.topology import ALL_QUEUES, declare_topology

logger = logging.getLogger("escrow.messaging")


@dataclass(frozen=True)
class BrokerQueueDepth:
    """One fixed-name RabbitMQ queue's safely observable depth."""

    queue_name: str
    message_count: int
    consumer_count: int


@dataclass(frozen=True)
class MessagingOperationalSnapshot:
    """The bounded values needed to alert on stalled asynchronous processing."""

    pending_outbox_count: int
    oldest_pending_outbox_age_seconds: int
    maximum_outbox_publish_attempts: int
    pending_dead_letter_records: int
    dead_letter_depth: int
    queue_depths: tuple[BrokerQueueDepth, ...]


def collect_broker_queue_depths(channel: Any) -> tuple[BrokerQueueDepth, ...]:
    """Passively inspect only the declared queues; no arbitrary broker lookup is allowed."""
    queue_depths: list[BrokerQueueDepth] = []
    for queue in ALL_QUEUES:
        declared = queue(channel).queue_declare(passive=True)
        try:
            _, message_count, consumer_count = declared
        except (TypeError, ValueError) as error:
            raise RuntimeError("broker queue declaration did not include depth metadata") from error
        queue_depths.append(
            BrokerQueueDepth(
                queue_name=queue.name,
                message_count=int(message_count),
                consumer_count=int(consumer_count),
            )
        )
    return tuple(queue_depths)


def collect_operational_snapshot(
    *,
    queue_depths: tuple[BrokerQueueDepth, ...] = (),
    now: datetime | None = None,
) -> MessagingOperationalSnapshot:
    """Collect durable outbox/DLQ signals without exposing business payloads."""
    current_time = now or timezone.now()
    pending = OutboxEvent.objects.filter(published_at__isnull=True)
    aggregates = pending.aggregate(
        oldest_occurred_at=Min("occurred_at"),
        maximum_publish_attempts=Max("publish_attempts"),
    )
    oldest = aggregates["oldest_occurred_at"]
    if isinstance(oldest, datetime):
        oldest_age_seconds = max(0, int((current_time - oldest).total_seconds()))
    else:
        oldest_age_seconds = 0
    dead_letter_depth = sum(
        queue.message_count for queue in queue_depths if queue.queue_name.endswith(".dlq")
    )
    maximum_publish_attempts = aggregates["maximum_publish_attempts"]
    return MessagingOperationalSnapshot(
        pending_outbox_count=pending.count(),
        oldest_pending_outbox_age_seconds=oldest_age_seconds,
        maximum_outbox_publish_attempts=(
            int(maximum_publish_attempts) if maximum_publish_attempts is not None else 0
        ),
        pending_dead_letter_records=DeadLetterMessage.objects.filter(
            replayed_at__isnull=True
        ).count(),
        dead_letter_depth=dead_letter_depth,
        queue_depths=queue_depths,
    )


def emit_operational_snapshot(
    *,
    queue_depths: tuple[BrokerQueueDepth, ...] = (),
    now: datetime | None = None,
) -> MessagingOperationalSnapshot:
    """Write a structured-safe operational signal with fixed, low-cardinality fields."""
    snapshot = collect_operational_snapshot(queue_depths=queue_depths, now=now)
    queue_depths_text = ",".join(
        f"{queue.queue_name}:{queue.message_count}" for queue in snapshot.queue_depths
    )
    logger.info(
        "messaging_operational_snapshot pending_outbox=%s oldest_outbox_age_seconds=%s "
        "max_outbox_publish_attempts=%s pending_dead_letter_records=%s "
        "dead_letter_depth=%s queue_depths=%s",
        snapshot.pending_outbox_count,
        snapshot.oldest_pending_outbox_age_seconds,
        snapshot.maximum_outbox_publish_attempts,
        snapshot.pending_dead_letter_records,
        snapshot.dead_letter_depth,
        queue_depths_text,
    )
    return snapshot


def emit_operational_snapshot_from_broker() -> MessagingOperationalSnapshot:
    """Inspect declared broker queues and emit their safe operational snapshot."""
    with confirming_broker_connection() as connection:
        channel = connection.channel()
        try:
            declare_topology(channel)
            queue_depths = collect_broker_queue_depths(channel)
            return emit_operational_snapshot(queue_depths=queue_depths)
        finally:
            channel.close()
