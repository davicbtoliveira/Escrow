"""Celery entrypoint for non-durable public checkout status refreshes."""

from __future__ import annotations

from typing import Any

from asgiref.sync import async_to_sync
from celery import shared_task  # type: ignore[import-untyped]
from channels.layers import get_channel_layer  # type: ignore[import-untyped]

from escrow.messaging.consumer import PermanentMessageError, consume_envelope_task
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.topology import NOTIFICATIONS_REALTIME_QUEUE, NOTIFICATIONS_WEBHOOK_QUEUE
from escrow.notifications.realtime import (
    AGREEMENT_STATUS_CHANGED_TYPE,
    agreement_status_group_name,
    channels_status_event,
    public_status_snapshot,
)


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    name="escrow.notifications.deliver_webhook",
    queue=NOTIFICATIONS_WEBHOOK_QUEUE.name,
    routing_key=NOTIFICATIONS_WEBHOOK_QUEUE.name,
)
def deliver_webhook(task: Any, body: object) -> bool:
    """Deliver one safe organization event; due retries are re-enqueued by Beat."""
    result = consume_envelope_task(
        task,
        body,
        expected_type="DeliverWebhookEvent.v1",
        expected_version=1,
        consumer=NOTIFICATIONS_WEBHOOK_QUEUE.name,
        effect=_deliver_webhook_event,
    )
    return result.processed


def _deliver_webhook_event(envelope: MessageEnvelope) -> None:
    from uuid import UUID

    from escrow.integrations.models import WebhookEvent
    from escrow.integrations.webhooks import deliver_webhook_event

    payload = envelope.payload
    if set(payload) != {"event_id"} or not isinstance(payload["event_id"], str):
        raise PermanentMessageError("webhook delivery payload is invalid")
    try:
        event_id = UUID(payload["event_id"])
        event = WebhookEvent.objects.get(id=event_id)
    except (ValueError, WebhookEvent.DoesNotExist) as error:
        raise PermanentMessageError("webhook delivery event is unknown") from error
    if str(event.organization_id) != envelope.tenant_id:
        raise PermanentMessageError("webhook delivery event is outside its tenant")
    deliver_webhook_event(event.id)


class RealtimeDeliveryUnavailable(RuntimeError):
    """The non-authoritative Redis channel layer is temporarily unavailable."""


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    name="escrow.notifications.publish_realtime",
    queue=NOTIFICATIONS_REALTIME_QUEUE.name,
    routing_key=NOTIFICATIONS_REALTIME_QUEUE.name,
)
def publish_realtime(task: Any, body: object) -> bool:
    """Publish one validated agreement-status event, deduplicated through the inbox."""
    result = consume_envelope_task(
        task,
        body,
        expected_type=AGREEMENT_STATUS_CHANGED_TYPE,
        expected_version=1,
        consumer="notifications.realtime",
        effect=_publish_status_event,
    )
    return result.processed


def _publish_status_event(envelope: MessageEnvelope) -> None:
    try:
        snapshot = public_status_snapshot(envelope.payload)
    except ValueError as error:
        raise PermanentMessageError("invalid realtime status payload") from error
    channel_layer = get_channel_layer()
    if channel_layer is None:
        raise RealtimeDeliveryUnavailable("channel layer is unavailable")
    async_to_sync(channel_layer.group_send)(
        agreement_status_group_name(snapshot["agreement_id"]),
        channels_status_event(snapshot),
    )
