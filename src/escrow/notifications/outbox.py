"""Transactional production of safe checkout-status refresh events."""

from __future__ import annotations

import uuid

from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.models import OutboxEvent
from escrow.messaging.outbox import enqueue_outbox_event
from escrow.messaging.topology import NOTIFICATIONS_REALTIME_QUEUE
from escrow.notifications.realtime import AGREEMENT_STATUS_CHANGED_TYPE

_STATUS_EVENT_NAMESPACE = uuid.UUID("f86c4462-b2f9-4b1c-b2f2-9c0c8e8be90d")


def enqueue_agreement_status_changed(
    agreement: EscrowAgreement,
    *,
    correlation_id: str,
    causation_id: str | None,
) -> OutboxEvent:
    """Enqueue a deterministic, PII-free snapshot inside the caller transaction."""
    if agreement.realtime_sequence < 1:
        raise ValueError("agreement status events need a positive sequence")
    envelope = MessageEnvelope.build(
        message_id=uuid.uuid5(
            _STATUS_EVENT_NAMESPACE,
            f"{agreement.id}:{agreement.realtime_sequence}:{agreement.status}",
        ),
        message_type=AGREEMENT_STATUS_CHANGED_TYPE,
        version=1,
        occurred_at=timezone.now(),
        correlation_id=correlation_id,
        causation_id=causation_id,
        tenant_id=str(agreement.organization_id),
        payload={
            "agreement_id": str(agreement.id),
            "status": agreement.status,
            "sequence": agreement.realtime_sequence,
        },
    )
    existing = OutboxEvent.objects.filter(id=envelope.message_id).first()
    if existing is not None:
        return existing
    return enqueue_outbox_event(envelope, routing_key=NOTIFICATIONS_REALTIME_QUEUE.name)
