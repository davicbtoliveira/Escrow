"""Replay one selected DLQ message with an attributable staff actor."""

from __future__ import annotations

from argparse import ArgumentParser
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from escrow.identity.models import User
from escrow.messaging.dlq import DeadLetterReplayError, replay_dead_letter_message
from escrow.messaging.models import DeadLetterMessage


class Command(BaseCommand):
    help = "Replay one selected dead-letter message with its original message identity."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("dead_letter_id")
        parser.add_argument("--actor-email", required=True)

    def handle(self, *args: object, **options: object) -> str:
        del args
        dead_letter_id = _dead_letter_id(options.get("dead_letter_id"))
        actor = _staff_actor(options.get("actor_email"))
        try:
            result = replay_dead_letter_message(dead_letter_id, actor=actor)
        except DeadLetterMessage.DoesNotExist as error:
            raise CommandError("Dead-letter message not found.") from error
        except DeadLetterReplayError as error:
            raise CommandError(str(error)) from error
        message = (
            f"Replayed dead-letter {result.dead_letter_id} "
            f"with original message {result.original_message_id}."
        )
        self.stdout.write(self.style.SUCCESS(message))
        return message


def _dead_letter_id(value: object) -> UUID:
    if not isinstance(value, str):
        raise CommandError("dead_letter_id must be a UUID.")
    try:
        return UUID(value)
    except ValueError as error:
        raise CommandError("dead_letter_id must be a UUID.") from error


def _staff_actor(value: object) -> User:
    if not isinstance(value, str) or not value:
        raise CommandError("--actor-email is required.")
    try:
        actor = User.objects.get(email=value.casefold())
    except User.DoesNotExist as error:
        raise CommandError("Staff actor not found.") from error
    if not actor.is_staff:
        raise CommandError("The replay actor must be staff.")
    return actor
