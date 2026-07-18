"""Transactional agreement creation and opaque hosted-checkout lookup."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.conf import settings
from django.db import transaction

from escrow.agreements.models import EscrowAgreement, IdempotencyRecord
from escrow.agreements.money import MoneyValidationError, format_minor_amount, parse_minor_amount
from escrow.agreements.pii import (
    CustomerIdentity,
    CustomerIdentityValidationError,
    EncryptedValue,
    PiiEncryptionUnavailable,
    blind_index,
    envelope_cipher,
    mask_document,
    mask_email,
    mask_name,
    validate_customer_identity,
)
from escrow.organizations.models import Organization

AGREEMENT_CREATE_ROUTE = "/api/v1/agreements/"
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")


class AgreementValidationError(ValueError):
    """The public agreement command is malformed or violates an MVP invariant."""


class IdempotencyKeyReusedError(RuntimeError):
    """One idempotency capability was presented with a conflicting command."""


class InactiveOrganizationError(RuntimeError):
    """A tenant was disabled after key authentication but before its mutation lock."""


class IdempotencyHashUnavailable(RuntimeError):
    """The dedicated HMAC secret is unavailable, so PII-bearing commands must stop."""


@dataclass(frozen=True)
class AgreementInput:
    external_customer_id: str
    customer: CustomerIdentity
    amount_minor: int
    currency: str
    delivery_window_days: int


@dataclass(frozen=True)
class AgreementCreationResult:
    status: int
    body: dict[str, object]
    replayed: bool


def parse_agreement_input(payload: dict[str, Any]) -> AgreementInput:
    """Validate the intentionally small agreement-creation command shape."""
    required_fields = {
        "external_customer_id",
        "customer",
        "amount",
        "currency",
        "delivery_window_days",
    }
    if set(payload) != required_fields or _contains_float(payload):
        raise AgreementValidationError("agreement payload is invalid")
    external_customer_id = payload["external_customer_id"]
    if (
        not isinstance(external_customer_id, str)
        or not (normalized_customer_id := external_customer_id.strip())
        or len(normalized_customer_id) > 128
    ):
        raise AgreementValidationError("external customer id is invalid")
    try:
        amount_minor, currency = parse_minor_amount(payload["amount"], payload["currency"])
        customer = validate_customer_identity(payload["customer"])
    except (MoneyValidationError, CustomerIdentityValidationError) as error:
        raise AgreementValidationError from error
    delivery_window_days = payload["delivery_window_days"]
    if type(delivery_window_days) is not int or not 1 <= delivery_window_days <= 90:
        raise AgreementValidationError("delivery window is invalid")
    return AgreementInput(
        external_customer_id=normalized_customer_id,
        customer=customer,
        amount_minor=amount_minor,
        currency=currency,
        delivery_window_days=delivery_window_days,
    )


def canonical_payload_hash(command: AgreementInput) -> str:
    """HMAC normalized terms without persisting an offline verifier for customer PII."""
    if not settings.AGREEMENT_IDEMPOTENCY_HMAC_SECRET:
        raise IdempotencyHashUnavailable("idempotency HMAC secret is not configured")
    payload = {
        "external_customer_id": command.external_customer_id,
        "customer": {
            "name": command.customer.name,
            "email": command.customer.email,
            "document": command.customer.document,
        },
        "amount": format_minor_amount(command.amount_minor),
        "currency": command.currency,
        "delivery_window_days": command.delivery_window_days,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hmac.new(
        settings.AGREEMENT_IDEMPOTENCY_HMAC_SECRET.encode(),
        encoded,
        hashlib.sha256,
    ).hexdigest()


def validate_idempotency_key(value: object) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise AgreementValidationError("idempotency key is invalid")
    return value


def create_agreement(
    *,
    organization_id: uuid.UUID,
    command: AgreementInput,
    payload_hash: str,
    idempotency_key: str,
) -> AgreementCreationResult:
    """Create one immutable agreement or replay its atomically stored response."""
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(id=organization_id)
        if not organization.is_active:
            raise InactiveOrganizationError
        existing = IdempotencyRecord.objects.filter(
            organization=organization,
            method="POST",
            route=AGREEMENT_CREATE_ROUTE,
            idempotency_key=idempotency_key,
        ).first()
        if existing is not None:
            if not hmac.compare_digest(existing.request_hash, payload_hash):
                raise IdempotencyKeyReusedError
            return AgreementCreationResult(
                status=existing.response_status,
                body=_response_with_checkout_url(
                    dict(existing.response_body),
                    _checkout_token_from_record(existing),
                ),
                replayed=True,
            )

        _validate_organization_fee(organization.fee_bps)
        agreement_id = uuid.uuid4()
        pii_context = customer_pii_context(organization.id, agreement_id)
        encrypted_customer = envelope_cipher().encrypt(command.customer.plaintext(), pii_context)
        checkout_token = new_checkout_token()
        agreement = EscrowAgreement.objects.create(
            id=agreement_id,
            organization=organization,
            external_customer_id=command.external_customer_id,
            customer_name_masked=mask_name(command.customer.name),
            customer_email_masked=mask_email(command.customer.email),
            customer_document_masked=mask_document(
                command.customer.document, command.customer.document_kind
            ),
            customer_document_kind=command.customer.document_kind,
            customer_email_blind_index=blind_index(command.customer.email, purpose="email"),
            customer_document_blind_index=blind_index(
                command.customer.document,
                purpose="document",
            ),
            customer_pii_ciphertext=encrypted_customer.ciphertext,
            customer_pii_nonce=encrypted_customer.nonce,
            customer_pii_encrypted_data_key=encrypted_customer.encrypted_data_key,
            customer_pii_kms_key_id=encrypted_customer.kms_key_id,
            checkout_token_hash=checkout_token_hash(checkout_token),
            amount_minor=command.amount_minor,
            currency=command.currency,
            fee_bps=organization.fee_bps,
            delivery_window_days=command.delivery_window_days,
        )
        record_id = uuid.uuid4()
        encrypted_checkout_token = envelope_cipher().encrypt(
            checkout_token.encode(),
            idempotency_checkout_token_context(organization.id, agreement.id, record_id),
        )
        response_body: dict[str, object] = {
            "agreement": agreement_payload(agreement),
        }
        IdempotencyRecord.objects.create(
            id=record_id,
            organization=organization,
            agreement=agreement,
            method="POST",
            route=AGREEMENT_CREATE_ROUTE,
            idempotency_key=idempotency_key,
            request_hash=payload_hash,
            response_status=201,
            response_body=response_body,
            checkout_token_ciphertext=encrypted_checkout_token.ciphertext,
            checkout_token_nonce=encrypted_checkout_token.nonce,
            checkout_token_encrypted_data_key=encrypted_checkout_token.encrypted_data_key,
            checkout_token_kms_key_id=encrypted_checkout_token.kms_key_id,
        )
        return AgreementCreationResult(
            status=201,
            body=_response_with_checkout_url(response_body, checkout_token),
            replayed=False,
        )


def find_checkout_agreement(checkout_token: str) -> EscrowAgreement | None:
    """Resolve only a correctly fingerprinted opaque bearer capability."""
    if not checkout_token.startswith("chk_") or len(checkout_token) > 255:
        return None
    return (
        EscrowAgreement.objects.select_related("organization")
        .filter(checkout_token_hash=checkout_token_hash(checkout_token))
        .first()
    )


def agreement_payload(agreement: EscrowAgreement) -> dict[str, object]:
    """Normal API representation: financial terms plus masked customer identity."""
    return {
        "id": str(agreement.id),
        "external_customer_id": agreement.external_customer_id,
        "status": agreement.status,
        "customer": {
            "name": agreement.customer_name_masked,
            "email_masked": agreement.customer_email_masked,
            "document_masked": agreement.customer_document_masked,
        },
        "amount": format_minor_amount(agreement.amount_minor),
        "currency": agreement.currency,
        "fee_bps": agreement.fee_bps,
        "delivery_window_days": agreement.delivery_window_days,
        "delivery_due_at": _isoformat(agreement.delivery_due_at),
        "inspection_deadline_at": _isoformat(agreement.inspection_deadline_at),
        "realtime_sequence": agreement.realtime_sequence,
    }


def public_checkout_payload(agreement: EscrowAgreement) -> dict[str, object]:
    """Strict public checkout whitelist; no capability or raw customer reference leaks."""
    payload = agreement_payload(agreement)
    payload.pop("external_customer_id")
    return {"agreement": payload}


def customer_pii_context(organization_id: uuid.UUID, agreement_id: uuid.UUID) -> dict[str, str]:
    """KMS context and AES-GCM AAD use immutable, non-PII identifiers only."""
    return {
        "service": "escrow",
        "purpose": "customer-pii",
        "organization_id": str(organization_id),
        "agreement_id": str(agreement_id),
        "version": "1",
    }


def idempotency_checkout_token_context(
    organization_id: uuid.UUID,
    agreement_id: uuid.UUID,
    idempotency_record_id: uuid.UUID,
) -> dict[str, str]:
    """Bind the encrypted checkout capability to one record without PII in AAD."""
    return {
        "service": "escrow",
        "purpose": "idempotency-checkout-token",
        "organization_id": str(organization_id),
        "agreement_id": str(agreement_id),
        "idempotency_record_id": str(idempotency_record_id),
        "version": "1",
    }


def checkout_url(checkout_token: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/checkout/{checkout_token}"


def new_checkout_token() -> str:
    return f"chk_{secrets.token_urlsafe(32)}"


def checkout_token_hash(checkout_token: str) -> str:
    if not settings.CHECKOUT_TOKEN_HMAC_SECRET:
        raise PiiEncryptionUnavailable("checkout-token key is not configured")
    return hmac.new(
        settings.CHECKOUT_TOKEN_HMAC_SECRET.encode(),
        checkout_token.encode(),
        hashlib.sha256,
    ).hexdigest()


def _checkout_token_from_record(record: IdempotencyRecord) -> str:
    ciphertext = record.checkout_token_ciphertext
    nonce = record.checkout_token_nonce
    encrypted_data_key = record.checkout_token_encrypted_data_key
    kms_key_id = record.checkout_token_kms_key_id
    if (
        ciphertext is None
        or nonce is None
        or encrypted_data_key is None
        or not isinstance(kms_key_id, str)
        or not kms_key_id
    ):
        raise PiiEncryptionUnavailable("idempotency record lacks an encrypted checkout token")
    encrypted = EncryptedValue(
        ciphertext=bytes(ciphertext),
        nonce=bytes(nonce),
        encrypted_data_key=bytes(encrypted_data_key),
        kms_key_id=kms_key_id,
    )
    try:
        token = (
            envelope_cipher()
            .decrypt(
                encrypted,
                idempotency_checkout_token_context(
                    record.organization_id,
                    record.agreement_id,
                    record.id,
                ),
            )
            .decode("ascii")
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise PiiEncryptionUnavailable("idempotency checkout token cannot be decrypted") from error
    if not token.startswith("chk_") or len(token) > 255:
        raise PiiEncryptionUnavailable("idempotency checkout token is invalid")
    return token


def _response_with_checkout_url(
    response_body: dict[str, object],
    checkout_token: str,
) -> dict[str, object]:
    response = dict(response_body)
    response.pop("checkout_url", None)
    response["checkout_url"] = checkout_url(checkout_token)
    return response


def _validate_organization_fee(fee_bps: int) -> None:
    if not 0 <= fee_bps <= 10_000:
        raise AgreementValidationError("organization fee is invalid")


def _contains_float(value: object) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_contains_float(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_float(item) for item in value)
    return False


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")
