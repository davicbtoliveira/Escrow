"""Durable DLQ metadata and explicit, idempotent replay boundaries."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from escrow.audit.services import record_audit_event
from escrow.identity.models import User
from escrow.messaging.envelope import EnvelopeValidationError, MessageEnvelope
from escrow.messaging.models import DeadLetterMessage
from escrow.messaging.publisher import KombuOutboxPublisher, confirming_broker_connection
from escrow.messaging.topology import (
    dead_letter_queue_for_routing_key,
    declare_topology,
    queue_for_routing_key,
)

logger = logging.getLogger("escrow.messaging")


class DeadLetterReplayError(RuntimeError):
    """An explicit replay cannot safely be completed."""


@dataclass(frozen=True)
class DeadLetterReplayResult:
    dead_letter_id: UUID
    original_message_id: UUID


class DeadLetterTransport(Protocol):
    """Moves a selected physical DLQ message back to its declared primary queue."""

    def replay(self, dead_letter: DeadLetterMessage, envelope: MessageEnvelope) -> None: ...


def capture_dead_letter_message(
    *,
    body: object,
    routing_key: str,
    error: str,
    attempt_count: int,
    source_task_id: str = "",
    headers: Mapping[str, object] | None = None,
) -> DeadLetterMessage:
    """Persist inspectable failure metadata before RabbitMQ dead-letters the task."""
    queue_for_routing_key(routing_key)
    if type(attempt_count) is not int or attempt_count < 1:
        raise ValueError("attempt_count must be a positive integer")
    if not isinstance(error, str) or not error or len(error) > 128:
        raise ValueError("error must be a non-empty string up to 128 characters")
    if not isinstance(source_task_id, str) or len(source_task_id) > 128:
        raise ValueError("source_task_id must be a string up to 128 characters")

    serialized_body = _json_value(body)
    serialized_headers = _json_object(headers or {})
    envelope = _parse_envelope(serialized_body)
    dead_letter = DeadLetterMessage.objects.create(
        original_message_id=envelope.message_id if envelope else None,
        source_task_id=source_task_id,
        routing_key=routing_key,
        message_type=envelope.message_type if envelope else "",
        version=envelope.version if envelope else None,
        correlation_id=envelope.correlation_id if envelope else "",
        causation_id=envelope.causation_id if envelope and envelope.causation_id else "",
        tenant_id=envelope.tenant_id if envelope else "",
        body=serialized_body,
        headers=serialized_headers,
        error=error,
        attempt_count=attempt_count,
    )
    logger.warning(
        "messaging_dead_lettered route=%s error=%s attempt=%s",
        routing_key,
        error,
        attempt_count,
        extra={
            "event_id": str(dead_letter.id),
            "correlation_id": dead_letter.correlation_id or None,
            "causation_id": dead_letter.causation_id or None,
        },
    )
    return dead_letter


def replay_dead_letter_message(
    dead_letter_id: UUID,
    *,
    actor: User,
    transport: DeadLetterTransport | None = None,
) -> DeadLetterReplayResult:
    """Replay one selected DLQ message once after an attributable operator action."""
    if not actor.is_staff:
        raise PermissionDenied("only staff users can replay dead-letter messages")
    replay_transport = transport or KombuDeadLetterTransport()
    failure: Exception | None = None
    result: DeadLetterReplayResult | None = None

    with transaction.atomic():
        dead_letter = DeadLetterMessage.objects.select_for_update().get(id=dead_letter_id)
        if dead_letter.replayed_at is not None:
            raise DeadLetterReplayError("dead-letter message was already replayed")
        envelope = _replayable_envelope(dead_letter)
        dead_letter.replay_attempts += 1
        try:
            replay_transport.replay(dead_letter, envelope)
        except Exception as error:
            dead_letter.last_replay_error = type(error).__name__
            dead_letter.save(update_fields=["replay_attempts", "last_replay_error"])
            record_audit_event(
                event_type="dead_letter_replay_failed",
                actor=actor,
                correlation_id=dead_letter.correlation_id,
                payload=_audit_payload(dead_letter),
            )
            failure = error
        else:
            dead_letter.replayed_at = timezone.now()
            dead_letter.last_replay_error = ""
            dead_letter.save(update_fields=["replay_attempts", "replayed_at", "last_replay_error"])
            record_audit_event(
                event_type="dead_letter_replayed",
                actor=actor,
                correlation_id=dead_letter.correlation_id,
                payload=_audit_payload(dead_letter),
            )
            result = DeadLetterReplayResult(
                dead_letter_id=dead_letter.id,
                original_message_id=envelope.message_id,
            )

    if failure is not None:
        logger.warning(
            "messaging_dead_letter_replay_failed route=%s error=%s",
            dead_letter.routing_key,
            type(failure).__name__,
            extra={
                "event_id": str(dead_letter.id),
                "correlation_id": dead_letter.correlation_id or None,
                "causation_id": dead_letter.causation_id or None,
            },
        )
        raise DeadLetterReplayError("dead-letter replay could not be published") from failure

    assert result is not None
    logger.info(
        "messaging_dead_letter_replayed route=%s attempt=%s",
        dead_letter.routing_key,
        dead_letter.replay_attempts,
        extra={
            "event_id": str(dead_letter.id),
            "correlation_id": dead_letter.correlation_id or None,
            "causation_id": dead_letter.causation_id or None,
        },
    )
    return result


class KombuDeadLetterTransport:
    """Replay only the selected head message and acknowledge it after a confirmed publish."""

    def __init__(self, publisher: KombuOutboxPublisher | None = None) -> None:
        self._publisher = publisher or KombuOutboxPublisher()

    def replay(self, dead_letter: DeadLetterMessage, envelope: MessageEnvelope) -> None:
        queue = dead_letter_queue_for_routing_key(dead_letter.routing_key)
        with confirming_broker_connection() as connection:
            channel = connection.channel()
            try:
                declare_topology(channel)
                message = queue(channel).get(no_ack=False)
                if message is None:
                    raise DeadLetterReplayError("selected DLQ is empty")
                try:
                    queued_envelope = _envelope_from_celery_payload(message.payload)
                    if queued_envelope.message_id != envelope.message_id:
                        raise DeadLetterReplayError("selected message is not at the DLQ head")
                    self._publisher.publish(envelope, routing_key=dead_letter.routing_key)
                except Exception:
                    message.reject(requeue=True)
                    raise
                message.ack()
            finally:
                channel.close()


def _replayable_envelope(dead_letter: DeadLetterMessage) -> MessageEnvelope:
    envelope = _parse_envelope(dead_letter.body)
    if envelope is None or dead_letter.original_message_id is None:
        raise DeadLetterReplayError("dead-letter message has no valid replayable envelope")
    if envelope.message_id != dead_letter.original_message_id:
        raise DeadLetterReplayError("dead-letter identity does not match its envelope")
    queue_for_routing_key(dead_letter.routing_key)
    return envelope


def _envelope_from_celery_payload(payload: object) -> MessageEnvelope:
    if not isinstance(payload, (list, tuple)) or len(payload) != 3:
        raise DeadLetterReplayError("DLQ message has an unsupported Celery payload")
    args = payload[0]
    if not isinstance(args, (list, tuple)) or len(args) != 1:
        raise DeadLetterReplayError("DLQ message has an unsupported Celery arguments shape")
    try:
        return MessageEnvelope.from_dict(args[0])
    except EnvelopeValidationError as error:
        raise DeadLetterReplayError("DLQ message has an invalid envelope") from error


def _parse_envelope(body: object) -> MessageEnvelope | None:
    try:
        return MessageEnvelope.from_dict(body)
    except EnvelopeValidationError:
        return None


def _json_value(value: object) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError) as error:
        raise ValueError("dead-letter metadata must be JSON serializable") from error


def _json_object(value: Mapping[str, object]) -> dict[str, Any]:
    normalized = _json_value(dict(value))
    if not isinstance(normalized, dict):
        raise ValueError("dead-letter headers must be a JSON object")
    return normalized


def _audit_payload(dead_letter: DeadLetterMessage) -> dict[str, object]:
    return {
        "dead_letter_id": str(dead_letter.id),
        "message_id": str(dead_letter.original_message_id)
        if dead_letter.original_message_id
        else "",
        "routing_key": dead_letter.routing_key,
        "replay_attempt": dead_letter.replay_attempts,
    }
