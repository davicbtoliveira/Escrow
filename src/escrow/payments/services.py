"""Small transactional commands for simulated PIX, before async processing."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from django.db import transaction
from django.utils import timezone as django_timezone

from escrow.agreements.models import EscrowAgreement
from escrow.payments.callbacks import callback_timestamp_value, verify_sandbox_callback_signature
from escrow.payments.models import ProviderCallbackReceipt, SandboxPixCharge, Transfer

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,254}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROVIDER_REFERENCE = re.compile(r"^pix_[A-Za-z0-9_-]{16,120}$")


class PaymentValidationError(ValueError):
    """A payment-domain command is malformed."""


class ChargeIdempotencyConflict(PaymentValidationError):
    """A second command attempts to create another charge for one agreement."""


class InvalidChargeState(PaymentValidationError):
    """The agreement cannot begin a new simulated PIX funding flow."""


class UnknownProviderReference(PaymentValidationError):
    """The authenticated provider callback does not identify a local charge."""


class CallbackReplayConflict(PaymentValidationError):
    """A provider event ID was reused with a distinct signed body."""


class InvalidCallbackTransition(PaymentValidationError):
    """A valid provider callback contradicts an already terminal charge."""


class FundingTransferConflict(PaymentValidationError):
    """A pre-existing funding intent does not match the confirmed PIX charge."""


@dataclass(frozen=True)
class ChargeCreationResult:
    charge: SandboxPixCharge
    replayed: bool


@dataclass(frozen=True)
class SandboxPixCallback:
    event_id: str
    provider_reference: str
    outcome: str


@dataclass(frozen=True)
class CallbackRegistrationResult:
    receipt: ProviderCallbackReceipt
    charge: SandboxPixCharge
    transfer: Transfer | None
    duplicate: bool


def create_sandbox_pix_charge(
    *,
    agreement_id: uuid.UUID,
    idempotency_key: str,
) -> ChargeCreationResult:
    """Create the one charge for an agreement, or replay the original command.

    The only agreement transition owned here is ``AWAITING_PAYMENT`` to
    ``FUNDING_PROCESSING``.  Risk, ledger, and outbox work remain separate.
    """
    _validate_idempotency_key(idempotency_key)
    with transaction.atomic():
        agreement = EscrowAgreement.objects.select_for_update().get(id=agreement_id)
        existing = SandboxPixCharge.objects.filter(agreement=agreement).first()
        if existing is not None:
            if hmac.compare_digest(existing.idempotency_key, idempotency_key):
                return ChargeCreationResult(charge=existing, replayed=True)
            raise ChargeIdempotencyConflict("a PIX charge already exists for this agreement")
        if agreement.status != EscrowAgreement.Status.AWAITING_PAYMENT:
            raise InvalidChargeState("agreement is not awaiting payment")

        charge = SandboxPixCharge.objects.create(
            agreement=agreement,
            amount_minor=agreement.amount_minor,
            currency=agreement.currency,
            provider_reference=_new_provider_reference(),
            idempotency_key=idempotency_key,
        )
        agreement.status = EscrowAgreement.Status.FUNDING_PROCESSING
        agreement.version += 1
        agreement.realtime_sequence += 1
        agreement.save(update_fields=["status", "version", "realtime_sequence", "updated_at"])
        return ChargeCreationResult(charge=charge, replayed=False)


def record_sandbox_pix_callback(
    *,
    raw_body: bytes,
    signature: str,
    timestamp: str | int,
    signing_secret: str,
    now: datetime | None = None,
    max_age_seconds: int = 300,
) -> CallbackRegistrationResult:
    """Authenticate and durably deduplicate one simulated provider callback.

    A confirmed event creates the single ``FUNDING`` intent.  No ledger post,
    outbox event, or Celery task is emitted from this boundary.
    """
    verify_sandbox_callback_signature(
        signing_secret=signing_secret,
        timestamp=timestamp,
        raw_body=raw_body,
        signature=signature,
        now=now,
        max_age_seconds=max_age_seconds,
    )
    callback = parse_sandbox_pix_callback(raw_body)
    payload_hash = hashlib.sha256(raw_body).hexdigest()
    received_at = _received_at(now)
    signature_timestamp = callback_timestamp_value(timestamp)

    with transaction.atomic():
        existing = (
            ProviderCallbackReceipt.objects.select_related("charge", "transfer")
            .filter(
                provider=ProviderCallbackReceipt.Provider.SANDBOX_PIX,
                provider_event_id=callback.event_id,
            )
            .first()
        )
        if existing is not None:
            return _existing_callback_result(existing, payload_hash)

        try:
            charge = (
                SandboxPixCharge.objects.select_for_update()
                .select_related("agreement")
                .get(provider_reference=callback.provider_reference)
            )
        except SandboxPixCharge.DoesNotExist as error:
            raise UnknownProviderReference("provider reference is unknown") from error

        # The charge lock serializes callbacks for the same payment.  Recheck
        # event identity after waiting so a network retry remains a no-op.
        existing = (
            ProviderCallbackReceipt.objects.select_related("charge", "transfer")
            .filter(
                provider=ProviderCallbackReceipt.Provider.SANDBOX_PIX,
                provider_event_id=callback.event_id,
            )
            .first()
        )
        if existing is not None:
            return _existing_callback_result(existing, payload_hash)

        transfer = _apply_charge_outcome(charge, callback, received_at)
        receipt = ProviderCallbackReceipt.objects.create(
            provider=ProviderCallbackReceipt.Provider.SANDBOX_PIX,
            provider_event_id=callback.event_id,
            charge=charge,
            transfer=transfer,
            outcome=callback.outcome,
            payload_hash=payload_hash,
            signature_timestamp=signature_timestamp,
        )
        return CallbackRegistrationResult(
            receipt=receipt,
            charge=charge,
            transfer=transfer,
            duplicate=False,
        )


def parse_sandbox_pix_callback(raw_body: bytes) -> SandboxPixCallback:
    """Parse the intentionally PII-free sandbox callback payload shape."""
    if not isinstance(raw_body, bytes):
        raise PaymentValidationError("callback body is invalid")
    try:
        decoded: object = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PaymentValidationError("callback body is invalid") from error
    if not isinstance(decoded, dict) or set(decoded) != {
        "event_id",
        "provider_reference",
        "outcome",
    }:
        raise PaymentValidationError("callback body is invalid")
    event_id = decoded["event_id"]
    provider_reference = decoded["provider_reference"]
    outcome = decoded["outcome"]
    if not isinstance(event_id, str) or _EVENT_ID.fullmatch(event_id) is None:
        raise PaymentValidationError("callback event ID is invalid")
    if (
        not isinstance(provider_reference, str)
        or _PROVIDER_REFERENCE.fullmatch(provider_reference) is None
    ):
        raise PaymentValidationError("callback provider reference is invalid")
    if outcome not in ProviderCallbackReceipt.Outcome.values:
        raise PaymentValidationError("callback outcome is invalid")
    return SandboxPixCallback(
        event_id=event_id,
        provider_reference=provider_reference,
        outcome=outcome,
    )


def _validate_idempotency_key(value: str) -> None:
    if not isinstance(value, str) or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise PaymentValidationError("idempotency key is invalid")


def _new_provider_reference() -> str:
    return f"pix_{secrets.token_urlsafe(24)}"


def _received_at(now: datetime | None) -> datetime:
    if now is None:
        return django_timezone.now()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


def _existing_callback_result(
    receipt: ProviderCallbackReceipt,
    payload_hash: str,
) -> CallbackRegistrationResult:
    if not hmac.compare_digest(receipt.payload_hash, payload_hash):
        raise CallbackReplayConflict("provider event ID was reused")
    return CallbackRegistrationResult(
        receipt=receipt,
        charge=receipt.charge,
        transfer=receipt.transfer,
        duplicate=True,
    )


def _apply_charge_outcome(
    charge: SandboxPixCharge,
    callback: SandboxPixCallback,
    received_at: datetime,
) -> Transfer | None:
    if charge.status == SandboxPixCharge.Status.PENDING:
        charge.status = callback.outcome
        if callback.outcome == SandboxPixCharge.Status.CONFIRMED:
            charge.confirmed_at = received_at
            charge.save(update_fields=["status", "confirmed_at", "updated_at"])
            return _funding_transfer(charge, callback.event_id)
        charge.rejected_at = received_at
        charge.save(update_fields=["status", "rejected_at", "updated_at"])
        return None

    if charge.status != callback.outcome:
        raise InvalidCallbackTransition("callback conflicts with the charge outcome")
    if callback.outcome == SandboxPixCharge.Status.CONFIRMED:
        return _funding_transfer(charge, callback.event_id)
    return None


def _funding_transfer(charge: SandboxPixCharge, event_id: str) -> Transfer:
    existing = Transfer.objects.filter(
        agreement=charge.agreement,
        kind=Transfer.Kind.FUNDING,
    ).first()
    if existing is not None:
        if (
            existing.amount_minor != charge.amount_minor
            or existing.currency != charge.currency
            or existing.provider != Transfer.Provider.SANDBOX_PIX
            or existing.provider_reference != charge.provider_reference
        ):
            raise FundingTransferConflict("funding transfer differs from the confirmed charge")
        return existing
    return Transfer.objects.create(
        agreement=charge.agreement,
        kind=Transfer.Kind.FUNDING,
        amount_minor=charge.amount_minor,
        currency=charge.currency,
        provider=Transfer.Provider.SANDBOX_PIX,
        provider_reference=charge.provider_reference,
        provider_event_id=event_id,
        idempotency_key=f"provider-event:{event_id}",
    )
