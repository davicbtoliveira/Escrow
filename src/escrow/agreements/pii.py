"""Envelope encryption and safe normalization for external-customer identity."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from typing import Any, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email


class CustomerIdentityValidationError(ValueError):
    """A required customer identity field does not meet the public contract."""


class PiiEncryptionUnavailable(RuntimeError):
    """KMS could not protect PII and the request must not persist plaintext."""


@dataclass(frozen=True)
class CustomerIdentity:
    name: str
    email: str
    document: str
    document_kind: str

    def plaintext(self) -> bytes:
        """Create the one canonical, encrypted identity payload."""
        return json.dumps(
            {"name": self.name, "email": self.email, "document": self.document},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()


@dataclass(frozen=True)
class EncryptedValue:
    ciphertext: bytes
    nonce: bytes
    encrypted_data_key: bytes
    kms_key_id: str


class EnvelopeCipher(Protocol):
    def encrypt(self, plaintext: bytes, context: dict[str, str]) -> EncryptedValue: ...

    def decrypt(self, value: EncryptedValue, context: dict[str, str]) -> bytes: ...


def validate_customer_identity(value: object) -> CustomerIdentity:
    """Normalize and validate the fields required for an accountless customer."""
    if not isinstance(value, dict):
        raise CustomerIdentityValidationError("customer must be an object")
    name = value.get("name")
    email = value.get("email")
    document = value.get("document")
    if (
        not isinstance(name, str)
        or not (normalized_name := name.strip())
        or len(normalized_name) > 200
    ):
        raise CustomerIdentityValidationError("customer name is invalid")
    if not isinstance(email, str) or not (normalized_email := email.strip().casefold()):
        raise CustomerIdentityValidationError("customer email is invalid")
    try:
        validate_email(normalized_email)
    except ValidationError as error:
        raise CustomerIdentityValidationError("customer email is invalid") from error
    if not isinstance(document, str):
        raise CustomerIdentityValidationError("customer document is invalid")
    if re.search(r"[^0-9.\-/\s]", document):
        raise CustomerIdentityValidationError("customer document is invalid")
    normalized_document = re.sub(r"\D", "", document)
    document_kind = _document_kind(normalized_document)
    return CustomerIdentity(
        name=normalized_name,
        email=normalized_email,
        document=normalized_document,
        document_kind=document_kind,
    )


def _document_kind(document: str) -> str:
    if len(document) == 11 and _valid_cpf(document):
        return "CPF"
    if len(document) == 14 and _valid_cnpj(document):
        return "CNPJ"
    raise CustomerIdentityValidationError("customer document is invalid")


def _valid_cpf(document: str) -> bool:
    if len(set(document)) == 1:
        return False
    first = _cpf_digit(document[:9])
    second = _cpf_digit(document[:9] + str(first))
    return document[-2:] == f"{first}{second}"


def _cpf_digit(digits: str) -> int:
    weighted_sum = sum(
        int(digit) * weight for digit, weight in zip(digits, range(len(digits) + 1, 1, -1))
    )
    return (weighted_sum * 10 % 11) % 10


def _valid_cnpj(document: str) -> bool:
    if len(set(document)) == 1:
        return False
    first = _cnpj_digit(document[:12])
    second = _cnpj_digit(document[:12] + str(first))
    return document[-2:] == f"{first}{second}"


def _cnpj_digit(digits: str) -> int:
    weights = (
        (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
        if len(digits) == 12
        else (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    )
    remainder = sum(int(digit) * weight for digit, weight in zip(digits, weights)) % 11
    return 0 if remainder < 2 else 11 - remainder


def blind_index(value: str, *, purpose: str) -> str:
    """Correlate normalized PII without exposing it in indexes or query logs."""
    if not settings.PII_BLIND_INDEX_SECRET:
        raise PiiEncryptionUnavailable("PII blind-index secret is not configured")
    material = f"escrow:{purpose}:v1:{value}".encode()
    return hmac.new(settings.PII_BLIND_INDEX_SECRET.encode(), material, hashlib.sha256).hexdigest()


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if local else f"***@{domain}"


def mask_document(document: str, kind: str) -> str:
    if kind == "CPF":
        return f"***.***.***-{document[-2:]}"
    return f"**.***.***/****-{document[-2:]}"


def mask_name(name: str) -> str:
    parts = name.split()
    if len(parts) == 1:
        return f"{parts[0][0]}."
    return f"{parts[0]} {parts[-1][0]}."


def _aad(context: dict[str, str]) -> bytes:
    return json.dumps(context, separators=(",", ":"), sort_keys=True).encode()


class LocalEnvelopeCipher:
    """AES-GCM local substitute permitted only in DEBUG/test configurations."""

    key_id = "local-development-key"

    def __init__(self) -> None:
        if not settings.PII_LOCAL_MASTER_KEY:
            raise PiiEncryptionUnavailable("local PII master key is not configured")
        self.key = hashlib.sha256(settings.PII_LOCAL_MASTER_KEY.encode()).digest()

    def encrypt(self, plaintext: bytes, context: dict[str, str]) -> EncryptedValue:
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self.key).encrypt(nonce, plaintext, _aad(context))
        return EncryptedValue(
            ciphertext=ciphertext,
            nonce=nonce,
            encrypted_data_key=b"local-development-key",
            kms_key_id=self.key_id,
        )

    def decrypt(self, value: EncryptedValue, context: dict[str, str]) -> bytes:
        if value.encrypted_data_key != b"local-development-key":
            raise PiiEncryptionUnavailable("not a local encrypted value")
        try:
            return AESGCM(self.key).decrypt(value.nonce, value.ciphertext, _aad(context))
        except (InvalidTag, ValueError) as error:
            raise PiiEncryptionUnavailable("local customer identity cannot be decrypted") from error


class KmsEnvelopeCipher:
    """Generate a fresh AES-256 DEK in KMS and use it only for one PII blob."""

    def __init__(self) -> None:
        self.client: Any = boto3.client(
            "kms",
            endpoint_url=settings.MINISTACK_ENDPOINT_URL,
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        )

    def encrypt(self, plaintext: bytes, context: dict[str, str]) -> EncryptedValue:
        try:
            data_key = self.client.generate_data_key(
                KeyId=settings.PII_KMS_KEY_ID,
                KeySpec="AES_256",
                EncryptionContext=context,
            )
            plaintext_key = bytes(data_key["Plaintext"])
            encrypted_key = bytes(data_key["CiphertextBlob"])
            key_id = str(data_key.get("KeyId", settings.PII_KMS_KEY_ID))
        except (BotoCoreError, ClientError, KeyError, TypeError) as error:
            raise PiiEncryptionUnavailable from error
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(plaintext_key).encrypt(nonce, plaintext, _aad(context))
        return EncryptedValue(
            ciphertext=ciphertext,
            nonce=nonce,
            encrypted_data_key=encrypted_key,
            kms_key_id=key_id,
        )

    def decrypt(self, value: EncryptedValue, context: dict[str, str]) -> bytes:
        try:
            result = self.client.decrypt(
                CiphertextBlob=value.encrypted_data_key,
                KeyId=value.kms_key_id,
                EncryptionContext=context,
            )
            plaintext_key = bytes(result["Plaintext"])
        except (BotoCoreError, ClientError, KeyError, TypeError) as error:
            raise PiiEncryptionUnavailable from error
        try:
            return AESGCM(plaintext_key).decrypt(value.nonce, value.ciphertext, _aad(context))
        except (InvalidTag, ValueError) as error:
            raise PiiEncryptionUnavailable("KMS customer identity cannot be decrypted") from error


def envelope_cipher() -> EnvelopeCipher:
    """Choose KMS by default outside development; never silently fall back there."""
    backend = settings.PII_ENCRYPTION_BACKEND
    if backend == "kms":
        return KmsEnvelopeCipher()
    if backend == "local" and settings.PII_LOCAL_ENCRYPTION_ALLOWED:
        return LocalEnvelopeCipher()
    raise PiiEncryptionUnavailable("local PII encryption is disabled outside development or tests")
