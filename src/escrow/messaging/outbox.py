"""Transactional outbox publishing and inbox-based consumer deduplication."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from django.db import IntegrityError, transaction
from django.utils import timezone

from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.models import OutboxEvent, ProcessedMessage
from escrow.messaging.publisher import KombuOutboxPublisher
from escrow.messaging.topology import exchange_for_routing_key


class OutboxTransactionRequiredError(RuntimeError):
    """An event was not attached to the caller's business transaction."""


class ConfirmedMessagePublisher(Protocol):
    """A boundary that returns only after the broker confirms a publication."""

    def publish(self, envelope: MessageEnvelope, *, routing_key: str) -> None: ...


@dataclass(frozen=True)
class OutboxPublishResult:
    published: int
    failed: int


@dataclass(frozen=True)
class MessageProcessingResult:
    processed: bool
    duplicate: bool


def enqueue_outbox_event(envelope: MessageEnvelope, *, routing_key: str) -> OutboxEvent:
    """Record an event in the current business transaction before any broker I/O."""
    if not transaction.get_connection().in_atomic_block:
        raise OutboxTransactionRequiredError("outbox events require an atomic transaction")
    exchange_for_routing_key(routing_key)
    return OutboxEvent.objects.create(
        id=envelope.message_id,
        message_type=envelope.message_type,
        version=envelope.version,
        occurred_at=envelope.occurred_at,
        correlation_id=envelope.correlation_id,
        causation_id=envelope.causation_id,
        tenant_id=envelope.tenant_id,
        payload=envelope.payload,
        routing_key=routing_key,
    )


def publish_pending_outbox_events(
    *,
    publisher: ConfirmedMessagePublisher | None = None,
    batch_size: int = 100,
) -> OutboxPublishResult:
    """Publish one locked batch and leave failed events pending for a future retry."""
    if type(batch_size) is not int or not 1 <= batch_size <= 1_000:
        raise ValueError("batch_size must be an integer from 1 through 1000")
    confirmed_publisher = publisher or KombuOutboxPublisher()
    published = 0
    failed = 0
    with transaction.atomic():
        pending_events = list(
            OutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(published_at__isnull=True)
            .order_by("occurred_at", "id")[:batch_size]
        )
        for event in pending_events:
            try:
                confirmed_publisher.publish(
                    _envelope_from_event(event),
                    routing_key=event.routing_key,
                )
            except Exception as error:
                event.publish_attempts += 1
                event.last_error = type(error).__name__
                event.save(update_fields=["publish_attempts", "last_error"])
                failed += 1
                break
            event.publish_attempts += 1
            event.published_at = timezone.now()
            event.last_error = ""
            event.save(update_fields=["publish_attempts", "published_at", "last_error"])
            published += 1
    return OutboxPublishResult(published=published, failed=failed)


def process_message_once(
    envelope: MessageEnvelope,
    *,
    consumer: str,
    effect: Callable[[], None],
) -> MessageProcessingResult:
    """Run a durable effect once; duplicates are safe acknowledgements."""
    if not isinstance(consumer, str) or not consumer or len(consumer) > 128:
        raise ValueError("consumer must be a non-empty string up to 128 characters")
    with transaction.atomic():
        try:
            with transaction.atomic():
                ProcessedMessage.objects.create(message_id=envelope.message_id, consumer=consumer)
        except IntegrityError:
            return MessageProcessingResult(processed=False, duplicate=True)
        effect()
        return MessageProcessingResult(processed=True, duplicate=False)


def _envelope_from_event(event: OutboxEvent) -> MessageEnvelope:
    return MessageEnvelope.build(
        message_id=event.id,
        message_type=event.message_type,
        version=event.version,
        occurred_at=event.occurred_at,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        tenant_id=event.tenant_id,
        payload=event.payload,
    )
