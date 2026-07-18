"""Initial durable outbox and consumer inbox schema."""

from __future__ import annotations

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="OutboxEvent",
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
                ("message_type", models.CharField(max_length=160)),
                ("version", models.PositiveSmallIntegerField()),
                ("occurred_at", models.DateTimeField()),
                ("correlation_id", models.CharField(max_length=128)),
                ("causation_id", models.CharField(blank=True, max_length=128, null=True)),
                ("tenant_id", models.CharField(max_length=128)),
                ("payload", models.JSONField()),
                ("routing_key", models.CharField(max_length=128)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("publish_attempts", models.PositiveIntegerField(default=0)),
                ("last_error", models.CharField(blank=True, max_length=128)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["occurred_at", "id"]},
        ),
        migrations.CreateModel(
            name="ProcessedMessage",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("message_id", models.UUIDField(unique=True)),
                ("consumer", models.CharField(max_length=128)),
                ("processed_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddIndex(
            model_name="outboxevent",
            index=models.Index(
                fields=["published_at", "occurred_at"],
                name="messaging_outbox_pending_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboxevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("version__gte", 1)),
                name="messaging_outbox_version_positive",
            ),
        ),
    ]
