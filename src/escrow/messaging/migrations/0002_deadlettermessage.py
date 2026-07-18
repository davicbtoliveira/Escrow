"""Persist inspectable metadata for queue-specific dead-letter deliveries."""

from __future__ import annotations

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("messaging", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="DeadLetterMessage",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("original_message_id", models.UUIDField(blank=True, null=True)),
                ("source_task_id", models.CharField(blank=True, max_length=128)),
                ("routing_key", models.CharField(max_length=128)),
                ("message_type", models.CharField(blank=True, max_length=160)),
                ("version", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("correlation_id", models.CharField(blank=True, max_length=128)),
                ("causation_id", models.CharField(blank=True, max_length=128)),
                ("tenant_id", models.CharField(blank=True, max_length=128)),
                ("body", models.JSONField()),
                ("headers", models.JSONField(default=dict)),
                ("error", models.CharField(max_length=128)),
                ("attempt_count", models.PositiveSmallIntegerField()),
                ("replay_attempts", models.PositiveSmallIntegerField(default=0)),
                ("replayed_at", models.DateTimeField(blank=True, null=True)),
                ("last_replay_error", models.CharField(blank=True, max_length=128)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.AddIndex(
            model_name="deadlettermessage",
            index=models.Index(
                fields=["routing_key", "created_at"],
                name="messaging_dlq_route_created",
            ),
        ),
        migrations.AddIndex(
            model_name="deadlettermessage",
            index=models.Index(
                fields=["original_message_id"],
                name="messaging_dlq_message_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="deadlettermessage",
            constraint=models.CheckConstraint(
                condition=models.Q(("attempt_count__gte", 1)),
                name="messaging_dlq_attempt_positive",
            ),
        ),
    ]
