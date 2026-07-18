"""Celery entrypoint for non-durable public checkout status refreshes."""

from __future__ import annotations

from typing import Any

from asgiref.sync import async_to_sync
from celery import shared_task  # type: ignore[import-untyped]
from channels.layers import get_channel_layer  # type: ignore[import-untyped]

from escrow.messaging.consumer import PermanentMessageError, consume_envelope_task
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.topology import NOTIFICATIONS_REALTIME_QUEUE
from escrow.notifications.realtime import (
    AGREEMENT_STATUS_CHANGED_TYPE,
    agreement_status_group_name,
    channels_status_event,
    public_status_snapshot,
)


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
