"""Atomic bridge from a verified PIX callback into risk processing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction

from escrow.agreements.lifecycle import confirm_funding
from escrow.audit.services import record_audit_event
from escrow.ledger.models import LedgerTransaction
from escrow.ledger.services import LedgerEntryInput, LedgerPosting, post_ledger_transaction
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.outbox import enqueue_outbox_event
from escrow.messaging.topology import RISK_FUNDING_QUEUE
from escrow.notifications.outbox import enqueue_agreement_status_changed
from escrow.payments.models import Transfer
from escrow.payments.services import CallbackRegistrationResult, record_sandbox_pix_callback


@dataclass(frozen=True)
class FundingCallbackResult:
    callback: CallbackRegistrationResult
    transfer: Transfer | None
    received_ledger_transaction: LedgerTransaction | None


def process_sandbox_pix_callback(
    *,
    raw_body: bytes,
    signature: str,
    timestamp: str | int,
    signing_secret: str,
    correlation_id: str,
    now: datetime | None = None,
) -> FundingCallbackResult:
    """Commit confirmed PIX receipt, pending-risk posting, and risk command together."""
    with transaction.atomic():
        callback = record_sandbox_pix_callback(
            raw_body=raw_body,
            signature=signature,
            timestamp=timestamp,
            signing_secret=signing_secret,
            now=now,
        )
        if callback.duplicate or callback.transfer is None:
            return FundingCallbackResult(
                callback=callback,
                transfer=callback.transfer,
                received_ledger_transaction=None,
            )

        transfer = callback.transfer
        confirmed_at = callback.charge.confirmed_at
        if confirmed_at is None:
            raise RuntimeError("confirmed PIX charge lacks its confirmation timestamp")
        agreement = confirm_funding(transfer.agreement_id, confirmed_at=confirmed_at)
        transfer.status = Transfer.Status.PROCESSING
        transfer.save(update_fields=["status", "updated_at"])
        posting = post_ledger_transaction(
            LedgerPosting(
                transfer_id=transfer.id,
                kind=LedgerTransaction.Kind.FUNDING_RECEIVED,
                currency=transfer.currency,
                idempotency_key=f"funding-received:{transfer.id}",
                entries=(
                    LedgerEntryInput.debit(
                        "PIX_CLEARING",
                        transfer.amount_minor,
                        transfer.currency,
                    ),
                    LedgerEntryInput.credit(
                        "FUNDS_PENDING_RISK",
                        transfer.amount_minor,
                        transfer.currency,
                    ),
                ),
            )
        )
        envelope = MessageEnvelope.build(
            message_id=uuid.uuid4(),
            message_type="EvaluateFundingRisk.v1",
            version=1,
            occurred_at=confirmed_at,
            correlation_id=correlation_id,
            causation_id=callback.receipt.provider_event_id,
            tenant_id=str(agreement.organization_id),
            payload={"agreement_id": str(agreement.id), "transfer_id": str(transfer.id)},
        )
        enqueue_outbox_event(envelope, routing_key=RISK_FUNDING_QUEUE.name)
        enqueue_agreement_status_changed(
            agreement,
            correlation_id=correlation_id,
            causation_id=callback.receipt.provider_event_id,
        )
        record_audit_event(
            event_type="sandbox_pix_callback_confirmed",
            organization=agreement.organization,
            agreement=agreement,
            correlation_id=correlation_id,
            payload={
                "provider_event_id": callback.receipt.provider_event_id,
                "transfer_id": str(transfer.id),
            },
        )
        return FundingCallbackResult(
            callback=callback,
            transfer=transfer,
            received_ledger_transaction=posting.transaction,
        )
