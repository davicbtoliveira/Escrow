"""Safe Celery consumer helpers for strict envelopes and at-least-once effects."""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from typing import NoReturn, Protocol

from celery.exceptions import Reject  # type: ignore[import-untyped]

from escrow.messaging.dlq import capture_dead_letter_message
from escrow.messaging.envelope import EnvelopeValidationError, MessageEnvelope
from escrow.messaging.outbox import MessageProcessingResult, process_message_once

MAX_TRANSIENT_RETRIES = 5


class PermanentMessageError(ValueError):
    """A valid message cannot ever produce a valid domain effect."""


class TransientMessageError(RuntimeError):
    """A retriable infrastructure failure with no sensitive error detail."""


class TaskRequest(Protocol):
    retries: int


class RetryingTask(Protocol):
    request: TaskRequest

    def retry(self, *, exc: BaseException, countdown: float) -> NoReturn: ...


def consume_message_once(
    body: object,
    *,
    expected_type: str,
    expected_version: int,
    consumer: str,
    effect: Callable[[MessageEnvelope], None],
) -> MessageProcessingResult:
    """Validate, deduplicate, and execute one consumer effect in one transaction."""
    try:
        envelope = MessageEnvelope.from_dict(body)
    except EnvelopeValidationError as error:
        raise Reject("invalid_envelope", requeue=False) from error
    if envelope.message_type != expected_type or envelope.version != expected_version:
        raise Reject("unexpected_message_schema", requeue=False)
    try:
        return process_message_once(
            envelope,
            consumer=consumer,
            effect=lambda: effect(envelope),
        )
    except Reject:
        raise
    except PermanentMessageError as error:
        raise Reject(type(error).__name__, requeue=False) from error
    except TransientMessageError:
        raise
    except Exception as error:
        raise TransientMessageError(type(error).__name__) from error


def consume_envelope_task(
    task: RetryingTask,
    body: object,
    *,
    expected_type: str,
    expected_version: int,
    consumer: str,
    effect: Callable[[MessageEnvelope], None],
    jitter: Callable[[float], float] | None = None,
) -> MessageProcessingResult:
    """Consume a task with bounded exponential retry, then DLQ on exhaustion."""
    try:
        return consume_message_once(
            body,
            expected_type=expected_type,
            expected_version=expected_version,
            consumer=consumer,
            effect=effect,
        )
    except Reject as rejection:
        try:
            _capture_rejection(task, body=body, routing_key=consumer, rejection=rejection)
        except TransientMessageError as error:
            _retry_or_reject(task, body=body, routing_key=consumer, error=error, jitter=jitter)
            raise AssertionError("retry or rejection must not return")
        raise
    except TransientMessageError as error:
        _retry_or_reject(task, body=body, routing_key=consumer, error=error, jitter=jitter)
        raise AssertionError("retry or rejection must not return")


def _retry_or_reject(
    task: RetryingTask,
    *,
    body: object,
    routing_key: str,
    error: TransientMessageError,
    jitter: Callable[[float], float] | None,
) -> NoReturn:
    retry_count = task.request.retries
    if type(retry_count) is not int or retry_count < 0:
        raise RuntimeError("Celery task retry count is invalid") from error
    if retry_count >= MAX_TRANSIENT_RETRIES:
        rejection = Reject(f"retry_exhausted:{type(error).__name__}", requeue=False)
        try:
            _capture_rejection(task, body=body, routing_key=routing_key, rejection=rejection)
        except TransientMessageError as capture_error:
            raise Reject("dead_letter_metadata_unavailable", requeue=True) from capture_error
        raise rejection from error
    backoff = float(min(2**retry_count, 60))
    retry_jitter = (jitter or _jitter)(backoff)
    task.retry(exc=error, countdown=backoff + retry_jitter)
    raise AssertionError("Celery retry must not return")


def _capture_rejection(
    task: RetryingTask,
    *,
    body: object,
    routing_key: str,
    rejection: Reject,
) -> None:
    request = task.request
    retries = request.retries
    if type(retries) is not int or retries < 0:
        raise RuntimeError("Celery task retry count is invalid")
    headers = getattr(request, "headers", None)
    if not isinstance(headers, Mapping):
        headers = {}
    source_task_id = getattr(request, "id", "")
    if not isinstance(source_task_id, str):
        source_task_id = ""
    try:
        capture_dead_letter_message(
            body=body,
            routing_key=routing_key,
            error=str(rejection.reason),
            attempt_count=retries + 1,
            source_task_id=source_task_id,
            headers=headers,
        )
    except Exception as error:
        raise TransientMessageError("dead-letter metadata storage is unavailable") from error


def _jitter(backoff: float) -> float:
    return random.uniform(0, backoff)
