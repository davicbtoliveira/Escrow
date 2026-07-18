"""Celery boundary for explainable funding-risk evaluation."""

from __future__ import annotations

import uuid
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]
from django.utils import timezone

from escrow.messaging.consumer import PermanentMessageError, consume_envelope_task
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.models import OutboxEvent
from escrow.messaging.outbox import enqueue_outbox_event
from escrow.messaging.topology import LEDGER_FUNDING_QUEUE, RISK_FUNDING_QUEUE
from escrow.payments.models import Transfer
from escrow.risk.policy import FundingRiskOutcome
from escrow.risk.services import evaluate_funding_transfer

_POST_FUNDING_NAMESPACE = uuid.UUID("8d4e356f-468c-40d3-b058-35b0c3dbbde9")


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    name="escrow.risk.evaluate_funding_risk",
    queue=RISK_FUNDING_QUEUE.name,
    routing_key=RISK_FUNDING_QUEUE.name,
)
def evaluate_funding_risk(self: Any, body: object) -> bool:
    """Persist one risk report and enqueue custody only when its policy approves."""
    result = consume_envelope_task(
        self,
        body,
        expected_type="EvaluateFundingRisk.v1",
        expected_version=1,
        consumer=RISK_FUNDING_QUEUE.name,
        effect=_evaluate_funding_risk,
    )
    return result.processed


def _evaluate_funding_risk(envelope: MessageEnvelope) -> None:
    transfer = _funding_transfer_from(envelope)
    decision = evaluate_funding_transfer(transfer.id, now=timezone.now())
    if decision.outcome != FundingRiskOutcome.APPROVED:
        return

    message_id = uuid.uuid5(_POST_FUNDING_NAMESPACE, str(transfer.id))
    if OutboxEvent.objects.filter(id=message_id).exists():
        return
    enqueue_outbox_event(
        MessageEnvelope.build(
            message_id=message_id,
            message_type="PostFunding.v1",
            version=1,
            occurred_at=decision.evaluated_at,
            correlation_id=envelope.correlation_id,
            causation_id=str(envelope.message_id),
            tenant_id=envelope.tenant_id,
            payload={
                "agreement_id": str(transfer.agreement_id),
                "transfer_id": str(transfer.id),
            },
        ),
        routing_key=LEDGER_FUNDING_QUEUE.name,
    )


def _funding_transfer_from(envelope: MessageEnvelope) -> Transfer:
    payload = envelope.payload
    if set(payload) != {"agreement_id", "transfer_id"}:
        raise PermanentMessageError("funding risk payload is invalid")
    agreement_id = _uuid_payload_value(payload, "agreement_id")
    transfer_id = _uuid_payload_value(payload, "transfer_id")
    try:
        transfer = Transfer.objects.select_related("agreement__organization").get(id=transfer_id)
    except Transfer.DoesNotExist as error:
        raise PermanentMessageError("funding transfer is unknown") from error
    if (
        transfer.kind != Transfer.Kind.FUNDING
        or transfer.agreement_id != agreement_id
        or str(transfer.agreement.organization_id) != envelope.tenant_id
    ):
        raise PermanentMessageError("funding risk message is outside its tenant or intent")
    return transfer


def _uuid_payload_value(payload: dict[str, object], key: str) -> uuid.UUID:
    value = payload.get(key)
    if not isinstance(value, str):
        raise PermanentMessageError("funding risk payload is invalid")
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise PermanentMessageError("funding risk payload is invalid") from error
