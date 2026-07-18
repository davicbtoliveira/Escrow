"""Celery boundary for idempotent escrow custody postings."""

from __future__ import annotations

import uuid
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]

from escrow.agreements.lifecycle import AgreementStateConflict, mark_funds_held
from escrow.agreements.models import EscrowAgreement
from escrow.ledger.models import LedgerTransaction
from escrow.ledger.services import (
    LedgerEntryInput,
    LedgerPosting,
    LedgerPostingValidationError,
    post_ledger_transaction,
)
from escrow.messaging.consumer import PermanentMessageError, consume_envelope_task
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.topology import LEDGER_FUNDING_QUEUE
from escrow.notifications.outbox import enqueue_agreement_status_changed
from escrow.payments.models import Transfer
from escrow.risk.models import FundingRiskDecision
from escrow.risk.policy import FundingRiskOutcome


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    name="escrow.ledger.post_funding",
    queue=LEDGER_FUNDING_QUEUE.name,
    routing_key=LEDGER_FUNDING_QUEUE.name,
)
def post_funding(self: Any, body: object) -> bool:
    """Move approved pending-risk funds into escrow exactly once."""
    result = consume_envelope_task(
        self,
        body,
        expected_type="PostFunding.v1",
        expected_version=1,
        consumer=LEDGER_FUNDING_QUEUE.name,
        effect=_post_funding,
    )
    return result.processed


def _post_funding(envelope: MessageEnvelope) -> None:
    transfer = _approved_funding_transfer_from(envelope)
    if transfer.status == Transfer.Status.COMPLETED:
        if transfer.agreement.status != EscrowAgreement.Status.HELD:
            raise PermanentMessageError("completed funding agreement is not held")
        return
    if transfer.status != Transfer.Status.PROCESSING:
        raise PermanentMessageError("funding transfer is not ready for custody")
    try:
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=transfer.id,
                kind=LedgerTransaction.Kind.FUNDS_HELD,
                currency=transfer.currency,
                idempotency_key=f"funds-held:{transfer.id}",
                entries=(
                    LedgerEntryInput.debit(
                        "FUNDS_PENDING_RISK",
                        transfer.amount_minor,
                        transfer.currency,
                    ),
                    LedgerEntryInput.credit(
                        "ESCROW_LIABILITY",
                        transfer.amount_minor,
                        transfer.currency,
                    ),
                ),
            )
        )
        agreement = mark_funds_held(transfer.agreement_id)
    except (AgreementStateConflict, LedgerPostingValidationError) as error:
        raise PermanentMessageError("funding custody posting is invalid") from error
    transfer.status = Transfer.Status.COMPLETED
    transfer.save(update_fields=["status", "updated_at"])
    enqueue_agreement_status_changed(
        agreement,
        correlation_id=envelope.correlation_id,
        causation_id=str(envelope.message_id),
    )


def _approved_funding_transfer_from(envelope: MessageEnvelope) -> Transfer:
    payload = envelope.payload
    if set(payload) != {"agreement_id", "transfer_id"}:
        raise PermanentMessageError("funding custody payload is invalid")
    agreement_id = _uuid_payload_value(payload, "agreement_id")
    transfer_id = _uuid_payload_value(payload, "transfer_id")
    try:
        transfer = (
            Transfer.objects.select_for_update()
            .select_related("agreement__organization")
            .get(id=transfer_id)
        )
        decision = FundingRiskDecision.objects.get(transfer=transfer)
    except (Transfer.DoesNotExist, FundingRiskDecision.DoesNotExist) as error:
        raise PermanentMessageError("funding custody prerequisite is unknown") from error
    if (
        transfer.kind != Transfer.Kind.FUNDING
        or transfer.agreement_id != agreement_id
        or decision.outcome != FundingRiskOutcome.APPROVED
        or str(transfer.agreement.organization_id) != envelope.tenant_id
    ):
        raise PermanentMessageError("funding custody message is outside its tenant or policy")
    return transfer


def _uuid_payload_value(payload: dict[str, object], key: str) -> uuid.UUID:
    value = payload.get(key)
    if not isinstance(value, str):
        raise PermanentMessageError("funding custody payload is invalid")
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise PermanentMessageError("funding custody payload is invalid") from error
