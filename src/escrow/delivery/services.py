"""Transaction boundary for an organization's delivery declaration."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.pii import EncryptedValue, PiiEncryptionUnavailable, envelope_cipher
from escrow.agreements.services import customer_pii_context, find_checkout_agreement
from escrow.audit.services import record_audit_event
from escrow.delivery.emails import CustomerOtpDeliveryError, send_customer_acceptance_otp
from escrow.delivery.models import CustomerOtpChallenge, DeliveryReport
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.outbox import enqueue_outbox_event
from escrow.messaging.topology import LEDGER_REFUND_QUEUE, LEDGER_RELEASE_QUEUE
from escrow.notifications.outbox import enqueue_agreement_status_changed
from escrow.payments.models import Transfer

INSPECTION_WINDOW_DAYS = 7
OTP_MAX_VERIFICATION_ATTEMPTS = 5
_OTP_CODE = re.compile(r"^[0-9]{6}$")
_RELEASE_NAMESPACE = uuid.UUID("4d118b4b-35d6-42e8-9d66-cc3fac582c72")
_REFUND_NAMESPACE = uuid.UUID("7f2c9b41-8e5a-4c3d-9a1b-0e6d5f4c2b18")


class DeliveryAgreementNotFound(LookupError):
    """The API caller's organization does not own the requested agreement."""


class DeliveryStateConflict(RuntimeError):
    """An agreement cannot enter inspection from its present state."""


class DeliveryIdempotencyConflict(RuntimeError):
    """A second delivery declaration used another idempotency capability."""


class CustomerOtpChallengeNotFound(LookupError):
    """The checkout capability does not own the requested OTP challenge."""


class CustomerOtpStateConflict(RuntimeError):
    """The agreement or its OTP is no longer eligible for customer acceptance."""


class CustomerOtpVerificationFailed(RuntimeError):
    """The submitted OTP is malformed, expired, exhausted, or incorrect."""


class CustomerAcceptanceAuthorizationInvalid(RuntimeError):
    """The short-lived proof of email verification cannot authorize a release."""


@dataclass(frozen=True, slots=True)
class DeliveryReportResult:
    agreement: EscrowAgreement
    report: DeliveryReport
    replayed: bool


@dataclass(frozen=True, slots=True)
class CustomerOtpRequestResult:
    challenge: CustomerOtpChallenge


@dataclass(frozen=True, slots=True)
class CustomerOtpVerificationResult:
    challenge: CustomerOtpChallenge
    acceptance_token: str


@dataclass(frozen=True, slots=True)
class CustomerAcceptanceResult:
    agreement: EscrowAgreement
    transfer: Transfer
    replayed: bool


def report_delivery(
    *,
    organization_id: UUID,
    agreement_id: UUID,
    idempotency_key: str,
    correlation_id: str,
    now: datetime | None = None,
) -> DeliveryReportResult:
    """Start the immutable seven-calendar-day inspection window exactly once."""
    reported_at = _reported_at(now)
    with transaction.atomic():
        try:
            agreement = (
                EscrowAgreement.objects.select_for_update()
                .select_related("organization")
                .get(id=agreement_id, organization_id=organization_id)
            )
        except EscrowAgreement.DoesNotExist as error:
            raise DeliveryAgreementNotFound from error

        existing = DeliveryReport.objects.select_for_update().filter(agreement=agreement).first()
        if existing is not None:
            if existing.idempotency_key != idempotency_key:
                raise DeliveryIdempotencyConflict
            return DeliveryReportResult(agreement=agreement, report=existing, replayed=True)
        if agreement.status != EscrowAgreement.Status.HELD:
            raise DeliveryStateConflict

        inspection_deadline_at = reported_at + timedelta(days=INSPECTION_WINDOW_DAYS)
        agreement.status = EscrowAgreement.Status.INSPECTION
        agreement.inspection_deadline_at = inspection_deadline_at
        agreement.version += 1
        agreement.realtime_sequence += 1
        agreement.save(
            update_fields=[
                "status",
                "inspection_deadline_at",
                "version",
                "realtime_sequence",
                "updated_at",
            ]
        )
        report = DeliveryReport.objects.create(
            agreement=agreement,
            idempotency_key=idempotency_key,
            reported_at=reported_at,
            inspection_deadline_at=inspection_deadline_at,
        )
        enqueue_agreement_status_changed(
            agreement,
            correlation_id=correlation_id,
            causation_id=str(report.id),
        )
        record_audit_event(
            event_type="delivery_reported",
            organization=agreement.organization,
            agreement=agreement,
            correlation_id=correlation_id,
            payload={"delivery_report_id": str(report.id)},
        )
        return DeliveryReportResult(agreement=agreement, report=report, replayed=False)


def enqueue_expired_delivery_refunds(*, now: datetime | None = None, limit: int = 100) -> int:
    """Enqueue one refund command per held agreement past its delivery deadline."""
    if type(limit) is not int or not 1 <= limit <= 1_000:
        raise ValueError("expired delivery refund scan limit is invalid")
    scanned_at = _reported_at(now)
    candidates = (
        EscrowAgreement.objects.filter(
            status=EscrowAgreement.Status.HELD,
            delivery_due_at__lte=scanned_at,
        )
        .order_by("delivery_due_at", "id")
        .values_list("id", flat=True)[:limit]
    )
    enqueued = 0
    for agreement_id in candidates:
        enqueued += _enqueue_expired_delivery_refund(agreement_id, scanned_at)
    return enqueued


def _enqueue_expired_delivery_refund(agreement_id: UUID, scanned_at: datetime) -> int:
    with transaction.atomic():
        agreement = (
            EscrowAgreement.objects.select_for_update()
            .select_related("organization")
            .get(id=agreement_id)
        )
        if (
            agreement.status != EscrowAgreement.Status.HELD
            or agreement.delivery_due_at is None
            or agreement.delivery_due_at > scanned_at
        ):
            return 0
        transfer = Transfer.objects.create(
            agreement=agreement,
            kind=Transfer.Kind.REFUND,
            amount_minor=agreement.amount_minor,
            currency=agreement.currency,
            provider=Transfer.Provider.INTERNAL,
            provider_reference=f"delivery-expired-refund-{agreement.id.hex}",
            idempotency_key=f"delivery-expired-refund:{agreement.id}",
        )
        agreement.status = EscrowAgreement.Status.REFUND_PENDING
        agreement.version += 1
        agreement.realtime_sequence += 1
        agreement.save(update_fields=["status", "version", "realtime_sequence", "updated_at"])
        correlation_id = f"delivery-expired-refund:{agreement.id}"
        enqueue_outbox_event(
            MessageEnvelope.build(
                message_id=uuid.uuid5(_REFUND_NAMESPACE, str(transfer.id)),
                message_type="RefundFunds.v1",
                version=1,
                occurred_at=scanned_at,
                correlation_id=correlation_id,
                causation_id=str(agreement.id),
                tenant_id=str(agreement.organization_id),
                payload={"agreement_id": str(agreement.id), "transfer_id": str(transfer.id)},
            ),
            routing_key=LEDGER_REFUND_QUEUE.name,
        )
        enqueue_agreement_status_changed(
            agreement,
            correlation_id=correlation_id,
            causation_id=str(transfer.id),
        )
        record_audit_event(
            event_type="delivery_expired_refund_enqueued",
            organization=agreement.organization,
            agreement=agreement,
            correlation_id=correlation_id,
            payload={"refund_transfer_id": str(transfer.id)},
        )
        return 1


def _reported_at(now: datetime | None) -> datetime:
    if now is None:
        return timezone.now()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now


def request_customer_acceptance_otp(
    *,
    checkout_token: str,
    correlation_id: str,
    now: datetime | None = None,
) -> CustomerOtpRequestResult:
    """Issue and email a short-lived, hashed proof for delivery acceptance."""
    sent_at = _reported_at(now)
    with transaction.atomic():
        agreement = _locked_checkout_agreement(checkout_token)
        _require_open_inspection(agreement, sent_at)
        code = _new_otp_code()
        challenge = CustomerOtpChallenge.objects.create(
            agreement=agreement,
            code_hash=_otp_hash(uuid.uuid4(), code),
            sent_at=sent_at,
            expires_at=sent_at + timedelta(seconds=settings.CUSTOMER_OTP_TTL_SECONDS),
        )
        challenge.code_hash = _otp_hash(challenge.id, code)
        challenge.save(update_fields=["code_hash"])
        email = _decrypt_customer_email(agreement)
        try:
            send_customer_acceptance_otp(email, code)
        except CustomerOtpDeliveryError:
            raise
        record_audit_event(
            event_type="customer_acceptance_otp_sent",
            organization=agreement.organization,
            agreement=agreement,
            correlation_id=correlation_id,
            payload={"otp_challenge_id": str(challenge.id)},
        )
        return CustomerOtpRequestResult(challenge=challenge)


def verify_customer_acceptance_otp(
    *,
    checkout_token: str,
    challenge_id: UUID,
    code: str,
    now: datetime | None = None,
) -> CustomerOtpVerificationResult:
    """Verify an emailed code and return a one-time acceptance capability."""
    verified_at = _reported_at(now)
    if _OTP_CODE.fullmatch(code) is None:
        raise CustomerOtpVerificationFailed
    failed = False
    with transaction.atomic():
        agreement = _locked_checkout_agreement(checkout_token)
        try:
            challenge = CustomerOtpChallenge.objects.select_for_update().get(
                id=challenge_id,
                agreement=agreement,
            )
        except CustomerOtpChallenge.DoesNotExist as error:
            raise CustomerOtpChallengeNotFound from error
        _require_open_inspection(agreement, verified_at)
        if challenge.verified_at is not None:
            raise CustomerOtpStateConflict
        if (
            challenge.expires_at <= verified_at
            or challenge.verification_attempts >= OTP_MAX_VERIFICATION_ATTEMPTS
            or not hmac.compare_digest(challenge.code_hash, _otp_hash(challenge.id, code))
        ):
            challenge.verification_attempts += 1
            challenge.save(update_fields=["verification_attempts"])
            failed = True
            acceptance_token = ""
        else:
            acceptance_token = _new_acceptance_token()
            challenge.verification_attempts += 1
            challenge.verified_at = verified_at
            challenge.authorization_token_hash = _acceptance_token_hash(acceptance_token)
            challenge.authorization_expires_at = verified_at + timedelta(
                seconds=settings.CUSTOMER_OTP_TTL_SECONDS
            )
            challenge.save(
                update_fields=[
                    "verification_attempts",
                    "verified_at",
                    "authorization_token_hash",
                    "authorization_expires_at",
                ]
            )
    if failed:
        raise CustomerOtpVerificationFailed
    return CustomerOtpVerificationResult(challenge=challenge, acceptance_token=acceptance_token)


def accept_customer_delivery(
    *,
    checkout_token: str,
    challenge_id: UUID,
    acceptance_token: str,
    correlation_id: str,
    now: datetime | None = None,
) -> CustomerAcceptanceResult:
    """Move verified customer acceptance to the asynchronous release command once."""
    accepted_at = _reported_at(now)
    with transaction.atomic():
        agreement = _locked_checkout_agreement(checkout_token)
        try:
            challenge = CustomerOtpChallenge.objects.select_for_update().get(
                id=challenge_id,
                agreement=agreement,
            )
        except CustomerOtpChallenge.DoesNotExist as error:
            raise CustomerOtpChallengeNotFound from error
        _require_acceptance_authorization(challenge, acceptance_token, accepted_at)
        existing = Transfer.objects.select_for_update().filter(
            agreement=agreement,
            kind=Transfer.Kind.RELEASE,
        ).first()
        if existing is not None:
            if (
                challenge.consumed_at is not None
                and agreement.status
                in {EscrowAgreement.Status.RELEASE_PENDING, EscrowAgreement.Status.RELEASED}
            ):
                return CustomerAcceptanceResult(
                    agreement=agreement,
                    transfer=existing,
                    replayed=True,
                )
            raise CustomerOtpStateConflict
        _require_open_inspection(agreement, accepted_at)

        transfer = Transfer.objects.create(
            agreement=agreement,
            kind=Transfer.Kind.RELEASE,
            amount_minor=agreement.amount_minor,
            currency=agreement.currency,
            provider=Transfer.Provider.INTERNAL,
            provider_reference=f"release-{agreement.id.hex}",
            idempotency_key=f"customer-acceptance:{challenge.id}",
        )
        agreement.status = EscrowAgreement.Status.RELEASE_PENDING
        agreement.version += 1
        agreement.realtime_sequence += 1
        agreement.save(update_fields=["status", "version", "realtime_sequence", "updated_at"])
        challenge.consumed_at = accepted_at
        challenge.save(update_fields=["consumed_at"])
        message_id = uuid.uuid5(_RELEASE_NAMESPACE, str(transfer.id))
        enqueue_outbox_event(
            MessageEnvelope.build(
                message_id=message_id,
                message_type="ReleaseFunds.v1",
                version=1,
                occurred_at=accepted_at,
                correlation_id=correlation_id,
                causation_id=str(challenge.id),
                tenant_id=str(agreement.organization_id),
                payload={"agreement_id": str(agreement.id), "transfer_id": str(transfer.id)},
            ),
            routing_key=LEDGER_RELEASE_QUEUE.name,
        )
        enqueue_agreement_status_changed(
            agreement,
            correlation_id=correlation_id,
            causation_id=str(challenge.id),
        )
        record_audit_event(
            event_type="customer_delivery_accepted",
            organization=agreement.organization,
            agreement=agreement,
            correlation_id=correlation_id,
            payload={"release_transfer_id": str(transfer.id)},
        )
        return CustomerAcceptanceResult(agreement=agreement, transfer=transfer, replayed=False)


def _locked_checkout_agreement(checkout_token: str) -> EscrowAgreement:
    agreement = find_checkout_agreement(checkout_token)
    if agreement is None:
        raise DeliveryAgreementNotFound
    return (
        EscrowAgreement.objects.select_for_update()
        .select_related("organization")
        .get(id=agreement.id)
    )


def _require_open_inspection(agreement: EscrowAgreement, now: datetime) -> None:
    if (
        agreement.status != EscrowAgreement.Status.INSPECTION
        or agreement.inspection_deadline_at is None
        or agreement.inspection_deadline_at <= now
    ):
        raise CustomerOtpStateConflict


def _decrypt_customer_email(agreement: EscrowAgreement) -> str:
    plaintext = envelope_cipher().decrypt(
        EncryptedValue(
            ciphertext=bytes(agreement.customer_pii_ciphertext),
            nonce=bytes(agreement.customer_pii_nonce),
            encrypted_data_key=bytes(agreement.customer_pii_encrypted_data_key),
            kms_key_id=agreement.customer_pii_kms_key_id,
        ),
        customer_pii_context(agreement.organization_id, agreement.id),
    )
    try:
        value = json.loads(plaintext)
        email = value["email"]
    except (TypeError, ValueError, KeyError) as error:
        raise PiiEncryptionUnavailable("customer email cannot be decrypted") from error
    if not isinstance(email, str):
        raise PiiEncryptionUnavailable("customer email cannot be decrypted")
    return email


def _otp_hash(challenge_id: UUID, code: str) -> str:
    if not settings.CUSTOMER_OTP_HMAC_SECRET:
        raise PiiEncryptionUnavailable("customer OTP secret is not configured")
    material = f"customer-acceptance-otp:v1:{challenge_id}:{code}".encode()
    return hmac.new(
        settings.CUSTOMER_OTP_HMAC_SECRET.encode(),
        material,
        hashlib.sha256,
    ).hexdigest()


def _new_otp_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _new_acceptance_token() -> str:
    return f"otp_accept_{secrets.token_urlsafe(32)}"


def _acceptance_token_hash(token: str) -> str:
    if not settings.CUSTOMER_OTP_HMAC_SECRET:
        raise PiiEncryptionUnavailable("customer OTP secret is not configured")
    return hmac.new(
        settings.CUSTOMER_OTP_HMAC_SECRET.encode(),
        f"customer-acceptance-token:v1:{token}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _require_acceptance_authorization(
    challenge: CustomerOtpChallenge,
    acceptance_token: str,
    now: datetime,
) -> None:
    token_hash = challenge.authorization_token_hash
    if (
        not isinstance(acceptance_token, str)
        or token_hash is None
        or not hmac.compare_digest(token_hash, _acceptance_token_hash(acceptance_token))
        or challenge.authorization_expires_at is None
    ):
        raise CustomerAcceptanceAuthorizationInvalid
    if challenge.consumed_at is None and challenge.authorization_expires_at <= now:
        raise CustomerAcceptanceAuthorizationInvalid
