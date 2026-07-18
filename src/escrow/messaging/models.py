"""Durable outbox and inbox records for at-least-once message delivery."""

from __future__ import annotations

import uuid

from django.db import models


class OutboxEvent(models.Model):
    """A committed envelope waiting for a confirmed RabbitMQ publication."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message_type = models.CharField(max_length=160)
    version = models.PositiveSmallIntegerField()
    occurred_at = models.DateTimeField()
    correlation_id = models.CharField(max_length=128)
    causation_id = models.CharField(max_length=128, null=True, blank=True)
    tenant_id = models.CharField(max_length=128)
    payload = models.JSONField()
    routing_key = models.CharField(max_length=128)
    published_at = models.DateTimeField(null=True, blank=True)
    publish_attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["occurred_at", "id"]
        indexes = [
            models.Index(
                fields=["published_at", "occurred_at"],
                name="messaging_outbox_pending_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(version__gte=1),
                name="messaging_outbox_version_positive",
            ),
        ]


class ProcessedMessage(models.Model):
    """The inbox claim retained with a consumer's durable business effect."""

    id = models.BigAutoField(primary_key=True)
    message_id = models.UUIDField(unique=True)
    consumer = models.CharField(max_length=128)
    processed_at = models.DateTimeField(auto_now_add=True)


class DeadLetterMessage(models.Model):
    """A durable, safe-to-inspect mirror of one queue-specific DLQ delivery."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    original_message_id = models.UUIDField(null=True, blank=True)
    source_task_id = models.CharField(max_length=128, blank=True)
    routing_key = models.CharField(max_length=128)
    message_type = models.CharField(max_length=160, blank=True)
    version = models.PositiveSmallIntegerField(null=True, blank=True)
    correlation_id = models.CharField(max_length=128, blank=True)
    causation_id = models.CharField(max_length=128, blank=True)
    tenant_id = models.CharField(max_length=128, blank=True)
    body = models.JSONField()
    headers = models.JSONField(default=dict)
    error = models.CharField(max_length=128)
    attempt_count = models.PositiveSmallIntegerField()
    replay_attempts = models.PositiveSmallIntegerField(default=0)
    replayed_at = models.DateTimeField(null=True, blank=True)
    last_replay_error = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(
                fields=["routing_key", "created_at"],
                name="messaging_dlq_route_created",
            ),
            models.Index(
                fields=["original_message_id"],
                name="messaging_dlq_message_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(attempt_count__gte=1),
                name="messaging_dlq_attempt_positive",
            ),
        ]
