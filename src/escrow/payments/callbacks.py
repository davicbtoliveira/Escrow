"""Pure signing primitives for simulated PIX provider callbacks."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime


class CallbackSignatureError(ValueError):
    """The callback signature or its inputs are malformed or forged."""


class CallbackTimestampExpired(CallbackSignatureError):
    """The signed callback is outside the accepted replay window."""


def sign_sandbox_callback(signing_secret: str, timestamp: str | int, raw_body: bytes) -> str:
    """Return the HMAC-SHA256 signature for an exact timestamp/body pair."""
    return hmac.new(
        _validate_secret(signing_secret),
        _signature_payload(timestamp, raw_body),
        hashlib.sha256,
    ).hexdigest()


def verify_sandbox_callback_signature(
    *,
    signing_secret: str,
    timestamp: str | int,
    raw_body: bytes,
    signature: str,
    now: datetime | None = None,
    max_age_seconds: int = 300,
) -> None:
    """Validate an exact HMAC and a bounded timestamp replay window.

    ``timestamp`` is retained verbatim in the HMAC input, then parsed only for
    expiry validation.  This avoids accepting a signature for a normalized but
    different header value.
    """
    timestamp_text, timestamp_value = _parse_timestamp(timestamp)
    if type(max_age_seconds) is not int or max_age_seconds < 1:
        raise ValueError("max_age_seconds must be a positive integer")
    if not isinstance(signature, str) or len(signature) != 64:
        raise CallbackSignatureError("callback signature is invalid")

    expected = hmac.new(
        _validate_secret(signing_secret),
        _signature_payload(timestamp_text, raw_body),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise CallbackSignatureError("callback signature is invalid")

    current = _utc_now() if now is None else _as_utc(now)
    try:
        received_at = datetime.fromtimestamp(timestamp_value, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise CallbackSignatureError("callback timestamp is invalid") from error
    if abs((current - received_at).total_seconds()) > max_age_seconds:
        raise CallbackTimestampExpired("callback timestamp is outside the replay window")


def callback_timestamp_value(timestamp: str | int) -> int:
    """Parse a validated provider timestamp without normalizing its HMAC input."""
    return _parse_timestamp(timestamp)[1]


def _validate_secret(signing_secret: str) -> bytes:
    if not isinstance(signing_secret, str) or not signing_secret:
        raise CallbackSignatureError("callback signing secret is unavailable")
    return signing_secret.encode()


def _signature_payload(timestamp: str | int, raw_body: bytes) -> bytes:
    timestamp_text, _ = _parse_timestamp(timestamp)
    if not isinstance(raw_body, bytes):
        raise CallbackSignatureError("callback body is invalid")
    return timestamp_text.encode("ascii") + raw_body


def _parse_timestamp(timestamp: str | int) -> tuple[str, int]:
    if type(timestamp) is int:
        if timestamp < 0:
            raise CallbackSignatureError("callback timestamp is invalid")
        timestamp_text = str(timestamp)
    elif isinstance(timestamp, str) and timestamp.isascii() and timestamp.isdecimal():
        timestamp_text = timestamp
    else:
        raise CallbackSignatureError("callback timestamp is invalid")
    if not timestamp_text or len(timestamp_text) > 16:
        raise CallbackSignatureError("callback timestamp is invalid")
    try:
        return timestamp_text, int(timestamp_text)
    except ValueError as error:
        raise CallbackSignatureError("callback timestamp is invalid") from error


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)
