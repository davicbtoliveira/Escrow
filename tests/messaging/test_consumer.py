from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock, patch
from uuid import UUID

from celery.exceptions import Reject
from django.test import TestCase

from escrow.messaging.consumer import (
    PermanentMessageError,
    consume_envelope_task,
    consume_message_once,
)
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.models import DeadLetterMessage, ProcessedMessage


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


class ConsumerTaskTests(TestCase):
    def test_expected_envelope_runs_its_effect_once_and_acknowledges_duplicates(self) -> None:
        effect = Mock()
        body = funding_envelope().to_dict()

        first = consume_message_once(
            body,
            expected_type="EvaluateFundingRisk.v1",
            expected_version=1,
            consumer="risk.funding",
            effect=effect,
        )
        duplicate = consume_message_once(
            body,
            expected_type="EvaluateFundingRisk.v1",
            expected_version=1,
            consumer="risk.funding",
            effect=effect,
        )

        assert first.processed
        assert duplicate.duplicate
        effect.assert_called_once_with(funding_envelope())
        processed_messages = ProcessedMessage.objects.filter(
            message_id=funding_envelope().message_id
        )
        assert processed_messages.count() == 1

    def test_invalid_or_unexpected_envelope_is_rejected_to_the_queue_dlq(self) -> None:
        effect = Mock()
        malformed = funding_envelope().to_dict()
        malformed["version"] = 2
        malformed["type"] = "EvaluateFundingRisk.v2"

        with self.assertRaises(Reject) as raised:
            consume_message_once(
                malformed,
                expected_type="EvaluateFundingRisk.v1",
                expected_version=1,
                consumer="risk.funding",
                effect=effect,
            )

        assert not raised.exception.requeue
        assert raised.exception.reason == "unexpected_message_schema"
        effect.assert_not_called()

    def test_permanent_effect_error_rejects_and_rolls_back_its_inbox_claim(self) -> None:
        def permanent_failure(_: MessageEnvelope) -> None:
            raise PermanentMessageError("invalid business state")

        with self.assertRaises(Reject) as raised:
            consume_message_once(
                funding_envelope().to_dict(),
                expected_type="EvaluateFundingRisk.v1",
                expected_version=1,
                consumer="risk.funding",
                effect=permanent_failure,
            )

        assert not raised.exception.requeue
        assert raised.exception.reason == "PermanentMessageError"
        processed_messages = ProcessedMessage.objects.filter(
            message_id=funding_envelope().message_id
        )
        assert not processed_messages.exists()

    def test_permanent_task_failure_is_captured_with_replay_metadata_before_dlq_rejection(
        self,
    ) -> None:
        class Task:
            class Request:
                retries = 0
                id = "9f59643f-9685-40c4-a7a0-e0c8d805e527"
                headers = {"traceparent": "00-portfolio-trace-01"}

            request = Request()

            def retry(self, *, exc: BaseException, countdown: float) -> None:
                del exc, countdown
                raise AssertionError("a permanent error must not retry")

        def permanent_failure(_: MessageEnvelope) -> None:
            raise PermanentMessageError("invalid business state")

        with self.assertRaises(Reject) as raised:
            consume_envelope_task(
                Task(),
                funding_envelope().to_dict(),
                expected_type="EvaluateFundingRisk.v1",
                expected_version=1,
                consumer="risk.funding",
                effect=permanent_failure,
            )

        assert not raised.exception.requeue
        dead_letter = DeadLetterMessage.objects.get()
        assert dead_letter.original_message_id == funding_envelope().message_id
        assert dead_letter.routing_key == "risk.funding"
        assert dead_letter.body == funding_envelope().to_dict()
        assert dead_letter.headers == {"traceparent": "00-portfolio-trace-01"}
        assert dead_letter.error == "PermanentMessageError"
        assert dead_letter.attempt_count == 1
        assert dead_letter.source_task_id == "9f59643f-9685-40c4-a7a0-e0c8d805e527"

    @patch("escrow.messaging.consumer.capture_dead_letter_message", side_effect=RuntimeError)
    def test_dlq_metadata_database_outage_retries_without_losing_the_primary_message(
        self,
        _: Mock,
    ) -> None:
        class RetryRequested(Exception):
            pass

        class Task:
            class Request:
                retries = 0

            request = Request()

            def __init__(self) -> None:
                self.retry = Mock(side_effect=RetryRequested())

        task = Task()

        def permanent_failure(_: MessageEnvelope) -> None:
            raise PermanentMessageError("invalid business state")

        with self.assertRaises(RetryRequested):
            consume_envelope_task(
                task,
                funding_envelope().to_dict(),
                expected_type="EvaluateFundingRisk.v1",
                expected_version=1,
                consumer="risk.funding",
                effect=permanent_failure,
                jitter=lambda _: 0,
            )

        assert type(task.retry.call_args.kwargs["exc"]).__name__ == "TransientMessageError"
        assert task.retry.call_args.kwargs["countdown"] == 1
        assert not DeadLetterMessage.objects.exists()

    @patch("escrow.messaging.consumer.capture_dead_letter_message", side_effect=RuntimeError)
    def test_exhausted_retry_requeues_when_dlq_metadata_cannot_be_persisted(
        self,
        _: Mock,
    ) -> None:
        class Task:
            class Request:
                retries = 5

            request = Request()
            retry = Mock()

        def transient_failure(_: MessageEnvelope) -> None:
            raise RuntimeError("database unavailable")

        with self.assertRaises(Reject) as raised:
            consume_envelope_task(
                Task(),
                funding_envelope().to_dict(),
                expected_type="EvaluateFundingRisk.v1",
                expected_version=1,
                consumer="risk.funding",
                effect=transient_failure,
            )

        assert raised.exception.requeue
        assert raised.exception.reason == "dead_letter_metadata_unavailable"
        Task.retry.assert_not_called()

    def test_transient_failure_uses_bounded_backoff_retry(self) -> None:
        class RetryRequested(Exception):
            pass

        class Task:
            class Request:
                retries = 0

            request = Request()

            def __init__(self) -> None:
                self.retry = Mock(side_effect=RetryRequested())

        task = Task()

        def transient_failure(_: MessageEnvelope) -> None:
            raise RuntimeError("database unavailable")

        with self.assertRaises(RetryRequested):
            consume_envelope_task(
                task,
                funding_envelope().to_dict(),
                expected_type="EvaluateFundingRisk.v1",
                expected_version=1,
                consumer="risk.funding",
                effect=transient_failure,
                jitter=lambda _: 0.25,
            )

        retry_kwargs = task.retry.call_args.kwargs
        assert retry_kwargs["countdown"] == 1.25
        assert type(retry_kwargs["exc"]).__name__ == "TransientMessageError"

    def test_exhausted_transient_failure_is_rejected_to_dlq_without_another_retry(self) -> None:
        class Task:
            class Request:
                retries = 5

            request = Request()
            retry = Mock()

        def transient_failure(_: MessageEnvelope) -> None:
            raise RuntimeError("database unavailable")

        with self.assertRaises(Reject) as raised:
            consume_envelope_task(
                Task(),
                funding_envelope().to_dict(),
                expected_type="EvaluateFundingRisk.v1",
                expected_version=1,
                consumer="risk.funding",
                effect=transient_failure,
            )

        assert not raised.exception.requeue
        assert raised.exception.reason == "retry_exhausted:TransientMessageError"
        Task.retry.assert_not_called()
        dead_letter = DeadLetterMessage.objects.get()
        assert dead_letter.error == "retry_exhausted:TransientMessageError"
        assert dead_letter.attempt_count == 6
