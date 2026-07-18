from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from unittest.mock import patch
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase

from escrow.messaging.dlq import DeadLetterReplayResult, capture_dead_letter_message
from escrow.messaging.envelope import MessageEnvelope


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


class ReplayDeadLetterCommandTests(TestCase):
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
        )

    @patch("escrow.messaging.management.commands.replay_dead_letter.replay_dead_letter_message")
    def test_command_requires_an_attributable_staff_actor_and_selected_record(
        self,
        replay: object,
    ) -> None:
        replay.return_value = DeadLetterReplayResult(  # type: ignore[attr-defined]
            dead_letter_id=self.dead_letter.id,
            original_message_id=funding_envelope().message_id,
        )
        output = StringIO()

        call_command(
            "replay_dead_letter",
            str(self.dead_letter.id),
            "--actor-email",
            self.actor.email,
            stdout=output,
        )

        replay.assert_called_once_with(self.dead_letter.id, actor=self.actor)  # type: ignore[attr-defined]
        assert str(self.dead_letter.id) in output.getvalue()
        assert str(funding_envelope().message_id) in output.getvalue()

    def test_command_rejects_a_non_staff_actor_before_any_replay(self) -> None:
        actor = get_user_model().objects.create_user(
            email="operator@escrow.example",
            password="Uma senha forte e exclusiva 2026!",
        )

        with self.assertRaisesRegex(CommandError, "staff"):
            call_command(
                "replay_dead_letter",
                str(self.dead_letter.id),
                "--actor-email",
                actor.email,
            )
