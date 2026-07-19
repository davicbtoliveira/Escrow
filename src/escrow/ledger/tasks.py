"""Celery boundary for idempotent escrow custody postings."""

from __future__ import annotations

import uuid
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]

from escrow.agreements.lifecycle import (
    AgreementStateConflict,
    mark_funding_rejected,
    mark_funds_held,
)
from escrow.agreements.models import EscrowAgreement
from escrow.agreements.money import MoneyValidationError, calculate_release_fee_minor
from escrow.audit.services import record_audit_event
from escrow.ledger.models import LedgerTransaction
from escrow.ledger.services import (
    LedgerEntryInput,
    LedgerPosting,
    LedgerPostingValidationError,
    post_ledger_transaction,
)
from escrow.messaging.consumer import PermanentMessageError, consume_envelope_task
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.topology import (
    LEDGER_FUNDING_QUEUE,
    LEDGER_REFUND_QUEUE,
    LEDGER_RELEASE_QUEUE,
)
from escrow.notifications.outbox import enqueue_agreement_status_changed
from escrow.payments.models import Transfer
from escrow.risk.models import FundingRiskDecision, FundingRiskReview
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


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    name="escrow.ledger.release_funds",
    queue=LEDGER_RELEASE_QUEUE.name,
    routing_key=LEDGER_RELEASE_QUEUE.name,
)
def release_funds(self: Any, body: object) -> bool:
    """Post an accepted delivery's gross, fee, and net release exactly once."""
    result = consume_envelope_task(
        self,
        body,
        expected_type="ReleaseFunds.v1",
        expected_version=1,
        consumer=LEDGER_RELEASE_QUEUE.name,
        effect=_release_funds,
    )
    return result.processed


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    name="escrow.ledger.refund_funds",
    queue=LEDGER_REFUND_QUEUE.name,
    routing_key=LEDGER_REFUND_QUEUE.name,
)
def refund_funds(self: Any, body: object) -> bool:
    """Return custody or rejected funding through PIX clearing exactly once."""
    if _envelope_message_type(body) == "RefundFunds.v1":
        result = consume_envelope_task(
            self,
            body,
            expected_type="RefundFunds.v1",
            expected_version=1,
            consumer=LEDGER_REFUND_QUEUE.name,
            effect=_refund_expired_custody,
        )
    else:
        result = consume_envelope_task(
            self,
            body,
            expected_type="ReturnRejectedFunding.v1",
            expected_version=1,
            consumer=LEDGER_REFUND_QUEUE.name,
            effect=_return_rejected_funding,
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


def _release_funds(envelope: MessageEnvelope) -> None:
    transfer, agreement = _release_transfer_from(envelope)
    if transfer.status == Transfer.Status.COMPLETED:
        if agreement.status != EscrowAgreement.Status.RELEASED:
            raise PermanentMessageError("completed release agreement is not released")
        return
    if (
        transfer.status != Transfer.Status.PENDING
        or agreement.status != EscrowAgreement.Status.RELEASE_PENDING
    ):
        raise PermanentMessageError("release transfer is not ready for posting")
    try:
        fee_minor = calculate_release_fee_minor(agreement.amount_minor, agreement.fee_bps)
        net_minor = agreement.amount_minor - fee_minor
        entries = [
            LedgerEntryInput.debit("ESCROW_LIABILITY", transfer.amount_minor, transfer.currency),
        ]
        if net_minor > 0:
            entries.append(
                LedgerEntryInput.credit("ORGANIZATION_PAYABLE", net_minor, transfer.currency)
            )
        if fee_minor > 0:
            entries.append(
                LedgerEntryInput.credit("PLATFORM_FEE_REVENUE", fee_minor, transfer.currency)
            )
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=transfer.id,
                kind=LedgerTransaction.Kind.FUNDS_RELEASED,
                currency=transfer.currency,
                idempotency_key=f"funds-released:{transfer.id}",
                entries=tuple(entries),
            )
        )
    except (LedgerPostingValidationError, MoneyValidationError) as error:
        raise PermanentMessageError("release ledger posting is invalid") from error
    agreement.status = EscrowAgreement.Status.RELEASED
    agreement.version += 1
    agreement.realtime_sequence += 1
    agreement.save(update_fields=["status", "version", "realtime_sequence", "updated_at"])
    transfer.status = Transfer.Status.COMPLETED
    transfer.save(update_fields=["status", "updated_at"])
    enqueue_agreement_status_changed(
        agreement,
        correlation_id=envelope.correlation_id,
        causation_id=str(envelope.message_id),
    )
    record_audit_event(
        event_type="funds_released",
        organization=agreement.organization,
        agreement=agreement,
        correlation_id=envelope.correlation_id,
        payload={"release_transfer_id": str(transfer.id)},
    )


def _refund_expired_custody(envelope: MessageEnvelope) -> None:
    transfer, agreement = _refund_transfer_from(envelope)
    if transfer.status == Transfer.Status.COMPLETED:
        if agreement.status != EscrowAgreement.Status.REFUNDED:
            raise PermanentMessageError("completed refund agreement is not refunded")
        return
    if (
        transfer.status != Transfer.Status.PENDING
        or agreement.status != EscrowAgreement.Status.REFUND_PENDING
    ):
        raise PermanentMessageError("refund transfer is not ready for posting")
    try:
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=transfer.id,
                kind=LedgerTransaction.Kind.FUNDS_REFUNDED,
                currency=transfer.currency,
                idempotency_key=f"funds-refunded:{transfer.id}",
                entries=(
                    LedgerEntryInput.debit(
                        "ESCROW_LIABILITY",
                        transfer.amount_minor,
                        transfer.currency,
                    ),
                    LedgerEntryInput.credit(
                        "PIX_CLEARING",
                        transfer.amount_minor,
                        transfer.currency,
                    ),
                ),
            )
        )
    except LedgerPostingValidationError as error:
        raise PermanentMessageError("refund ledger posting is invalid") from error
    agreement.status = EscrowAgreement.Status.REFUNDED
    agreement.version += 1
    agreement.realtime_sequence += 1
    agreement.save(update_fields=["status", "version", "realtime_sequence", "updated_at"])
    transfer.status = Transfer.Status.COMPLETED
    transfer.save(update_fields=["status", "updated_at"])
    enqueue_agreement_status_changed(
        agreement,
        correlation_id=envelope.correlation_id,
        causation_id=str(envelope.message_id),
    )
    record_audit_event(
        event_type="funds_refunded",
        organization=agreement.organization,
        agreement=agreement,
        correlation_id=envelope.correlation_id,
        payload={"refund_transfer_id": str(transfer.id)},
    )


def _return_rejected_funding(envelope: MessageEnvelope) -> None:
    transfer = _rejected_funding_transfer_from(envelope)
    if transfer.status == Transfer.Status.FAILED:
        if transfer.agreement.status != EscrowAgreement.Status.FUNDING_REJECTED:
            raise PermanentMessageError("rejected funding agreement has an invalid status")
        return
    if transfer.status != Transfer.Status.PROCESSING:
        raise PermanentMessageError("rejected funding transfer is not ready for return")
    try:
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=transfer.id,
                kind=LedgerTransaction.Kind.FUNDING_REJECTED,
                currency=transfer.currency,
                idempotency_key=f"funding-rejected:{transfer.id}",
                entries=(
                    LedgerEntryInput.debit(
                        "FUNDS_PENDING_RISK",
                        transfer.amount_minor,
                        transfer.currency,
                    ),
                    LedgerEntryInput.credit(
                        "PIX_CLEARING",
                        transfer.amount_minor,
                        transfer.currency,
                    ),
                ),
            )
        )
        agreement = mark_funding_rejected(transfer.agreement_id)
    except (AgreementStateConflict, LedgerPostingValidationError) as error:
        raise PermanentMessageError("funding return posting is invalid") from error
    transfer.status = Transfer.Status.FAILED
    transfer.save(update_fields=["status", "updated_at"])
    enqueue_agreement_status_changed(
        agreement,
        correlation_id=envelope.correlation_id,
        causation_id=str(envelope.message_id),
    )
    record_audit_event(
        event_type="funding_rejected_returned",
        organization=agreement.organization,
        agreement=agreement,
        correlation_id=envelope.correlation_id,
        payload={"funding_transfer_id": str(transfer.id)},
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
        decision = FundingRiskDecision.objects.select_related("manual_review").get(
            transfer=transfer
        )
    except (Transfer.DoesNotExist, FundingRiskDecision.DoesNotExist) as error:
        raise PermanentMessageError("funding custody prerequisite is unknown") from error
    manual_approval = (
        hasattr(decision, "manual_review")
        and decision.manual_review.outcome == FundingRiskReview.Outcome.APPROVED
    )
    if (
        transfer.kind != Transfer.Kind.FUNDING
        or transfer.agreement_id != agreement_id
        or (decision.outcome != FundingRiskOutcome.APPROVED and not manual_approval)
        or str(transfer.agreement.organization_id) != envelope.tenant_id
    ):
        raise PermanentMessageError("funding custody message is outside its tenant or policy")
    return transfer


def _release_transfer_from(envelope: MessageEnvelope) -> tuple[Transfer, EscrowAgreement]:
    payload = envelope.payload
    if set(payload) != {"agreement_id", "transfer_id"}:
        raise PermanentMessageError("release payload is invalid")
    agreement_id = _uuid_payload_value(payload, "agreement_id")
    transfer_id = _uuid_payload_value(payload, "transfer_id")
    try:
        transfer = Transfer.objects.select_for_update().get(id=transfer_id)
        agreement = (
            EscrowAgreement.objects.select_for_update()
            .select_related("organization")
            .get(id=agreement_id)
        )
    except (EscrowAgreement.DoesNotExist, Transfer.DoesNotExist) as error:
        raise PermanentMessageError("release prerequisite is unknown") from error
    if (
        transfer.kind != Transfer.Kind.RELEASE
        or transfer.agreement_id != agreement.id
        or transfer.provider != Transfer.Provider.INTERNAL
        or transfer.amount_minor != agreement.amount_minor
        or transfer.currency != agreement.currency
        or str(agreement.organization_id) != envelope.tenant_id
    ):
        raise PermanentMessageError("release message is outside its tenant or intent")
    return transfer, agreement


def _refund_transfer_from(envelope: MessageEnvelope) -> tuple[Transfer, EscrowAgreement]:
    payload = envelope.payload
    if set(payload) != {"agreement_id", "transfer_id"}:
        raise PermanentMessageError("refund payload is invalid")
    agreement_id = _uuid_payload_value(payload, "agreement_id")
    transfer_id = _uuid_payload_value(payload, "transfer_id")
    try:
        transfer = Transfer.objects.select_for_update().get(id=transfer_id)
        agreement = (
            EscrowAgreement.objects.select_for_update()
            .select_related("organization")
            .get(id=agreement_id)
        )
    except (EscrowAgreement.DoesNotExist, Transfer.DoesNotExist) as error:
        raise PermanentMessageError("refund prerequisite is unknown") from error
    if (
        transfer.kind != Transfer.Kind.REFUND
        or transfer.agreement_id != agreement.id
        or transfer.provider != Transfer.Provider.INTERNAL
        or transfer.amount_minor != agreement.amount_minor
        or transfer.currency != agreement.currency
        or str(agreement.organization_id) != envelope.tenant_id
    ):
        raise PermanentMessageError("refund message is outside its tenant or intent")
    return transfer, agreement


def _rejected_funding_transfer_from(envelope: MessageEnvelope) -> Transfer:
    payload = envelope.payload
    if set(payload) != {"agreement_id", "transfer_id"}:
        raise PermanentMessageError("funding return payload is invalid")
    agreement_id = _uuid_payload_value(payload, "agreement_id")
    transfer_id = _uuid_payload_value(payload, "transfer_id")
    try:
        transfer = (
            Transfer.objects.select_for_update()
            .select_related("agreement__organization")
            .get(id=transfer_id)
        )
        decision = FundingRiskDecision.objects.select_related("manual_review").get(
            transfer=transfer
        )
    except (Transfer.DoesNotExist, FundingRiskDecision.DoesNotExist) as error:
        raise PermanentMessageError("funding return prerequisite is unknown") from error
    manual_rejection = (
        hasattr(decision, "manual_review")
        and decision.manual_review.outcome == FundingRiskReview.Outcome.REJECTED
    )
    if (
        transfer.kind != Transfer.Kind.FUNDING
        or transfer.agreement_id != agreement_id
        or str(transfer.agreement.organization_id) != envelope.tenant_id
        or (decision.outcome != FundingRiskOutcome.REJECTED and not manual_rejection)
    ):
        raise PermanentMessageError("funding return message is outside its tenant or policy")
    return transfer


def _envelope_message_type(body: object) -> str | None:
    if not isinstance(body, dict):
        return None
    message_type = body.get("type")
    return message_type if isinstance(message_type, str) else None


def _uuid_payload_value(payload: dict[str, object], key: str) -> uuid.UUID:
    value = payload.get(key)
    if not isinstance(value, str):
        raise PermanentMessageError("funding custody payload is invalid")
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise PermanentMessageError("funding custody payload is invalid") from error
