from __future__ import annotations

from datetime import UTC, datetime

import pytest

from escrow.payments.callbacks import (
    CallbackSignatureError,
    CallbackTimestampExpired,
    sign_sandbox_callback,
    verify_sandbox_callback_signature,
)


def test_callback_signature_accepts_timestamp_and_exact_raw_body() -> None:
    raw_body = b'{"event_id":"evt_001","status":"CONFIRMED"}'
    signature = sign_sandbox_callback("sandbox-signing-secret", "1720000000", raw_body)

    verify_sandbox_callback_signature(
        signing_secret="sandbox-signing-secret",
        timestamp="1720000000",
        raw_body=raw_body,
        signature=signature,
        now=datetime.fromtimestamp(1_720_000_001, tz=UTC),
    )


def test_callback_signature_rejects_a_mutated_body() -> None:
    raw_body = b'{"event_id":"evt_001","status":"CONFIRMED"}'
    signature = sign_sandbox_callback("sandbox-signing-secret", "1720000000", raw_body)

    with pytest.raises(CallbackSignatureError):
        verify_sandbox_callback_signature(
            signing_secret="sandbox-signing-secret",
            timestamp="1720000000",
            raw_body=b'{"event_id":"evt_001","status":"REJECTED"}',
            signature=signature,
            now=datetime.fromtimestamp(1_720_000_001, tz=UTC),
        )


def test_callback_signature_rejects_an_expired_timestamp_after_authentication() -> None:
    raw_body = b'{"event_id":"evt_001","status":"CONFIRMED"}'
    signature = sign_sandbox_callback("sandbox-signing-secret", "1720000000", raw_body)

    with pytest.raises(CallbackTimestampExpired):
        verify_sandbox_callback_signature(
            signing_secret="sandbox-signing-secret",
            timestamp="1720000000",
            raw_body=raw_body,
            signature=signature,
            now=datetime.fromtimestamp(1_720_000_301, tz=UTC),
        )


def test_callback_signature_rejects_invalid_timestamp_values() -> None:
    with pytest.raises(CallbackSignatureError):
        sign_sandbox_callback("sandbox-signing-secret", -1, b"{}")
