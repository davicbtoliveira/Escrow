from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from django.test import TestCase

from escrow.payments.callbacks import (
    CallbackSignatureError,
    CallbackTimestampExpired,
    sign_sandbox_callback,
)
from escrow.payments.models import ProviderCallbackReceipt, SandboxPixCharge, Transfer
from escrow.payments.services import (
    CallbackRegistrationResult,
    ChargeIdempotencyConflict,
    create_sandbox_pix_charge,
    record_sandbox_pix_callback,
)
from tests.payments.test_models import agreement

SIGNING_SECRET = "sandbox-signing-secret"
CALLBACK_TIME = datetime.fromtimestamp(1_720_000_000, tz=UTC)
CALLBACK_TIMESTAMP = "1720000000"


class SandboxPixChargeServiceTests(TestCase):
    def test_create_charge_snapshots_terms_moves_funding_to_processing_and_replays(self) -> None:
        escrow_agreement = agreement()

        created = create_sandbox_pix_charge(
            agreement_id=escrow_agreement.id,
            idempotency_key="pix-charge-001",
        )
        replay = create_sandbox_pix_charge(
            agreement_id=escrow_agreement.id,
            idempotency_key="pix-charge-001",
        )

        escrow_agreement.refresh_from_db()
        assert created.replayed is False
        assert replay.replayed is True
        assert replay.charge.id == created.charge.id
        assert created.charge.amount_minor == escrow_agreement.amount_minor
        assert created.charge.currency == escrow_agreement.currency
        assert created.charge.status == SandboxPixCharge.Status.PENDING
        assert escrow_agreement.status == "FUNDING_PROCESSING"
        assert SandboxPixCharge.objects.count() == 1

    def test_charge_rejects_a_second_idempotency_key_for_the_same_agreement(self) -> None:
        escrow_agreement = agreement()
        create_sandbox_pix_charge(
            agreement_id=escrow_agreement.id,
            idempotency_key="pix-charge-001",
        )

        with pytest.raises(ChargeIdempotencyConflict):
            create_sandbox_pix_charge(
                agreement_id=escrow_agreement.id,
                idempotency_key="pix-charge-002",
            )


class SandboxPixCallbackServiceTests(TestCase):
    def setUp(self) -> None:
        self.escrow_agreement = agreement()
        self.charge = create_sandbox_pix_charge(
            agreement_id=self.escrow_agreement.id,
            idempotency_key="pix-charge-callback-001",
        ).charge

    def callback_body(self, *, event_id: str = "evt_001", outcome: str = "CONFIRMED") -> bytes:
        return json.dumps(
            {
                "event_id": event_id,
                "provider_reference": self.charge.provider_reference,
                "outcome": outcome,
            },
            separators=(",", ":"),
        ).encode()

    def record(self, raw_body: bytes) -> CallbackRegistrationResult:
        return record_sandbox_pix_callback(
            raw_body=raw_body,
            signature=sign_sandbox_callback(SIGNING_SECRET, CALLBACK_TIMESTAMP, raw_body),
            timestamp=CALLBACK_TIMESTAMP,
            signing_secret=SIGNING_SECRET,
            now=CALLBACK_TIME,
        )

    def test_confirmed_callback_creates_one_funding_transfer_and_exact_duplicate_replays(
        self,
    ) -> None:
        raw_body = self.callback_body()

        created = self.record(raw_body)
        duplicate = self.record(raw_body)

        self.charge.refresh_from_db()
        assert created.duplicate is False
        assert duplicate.duplicate is True
        assert created.receipt.id == duplicate.receipt.id
        assert self.charge.status == SandboxPixCharge.Status.CONFIRMED
        assert Transfer.objects.count() == 1
        transfer = Transfer.objects.get()
        assert transfer.kind == Transfer.Kind.FUNDING
        assert transfer.status == Transfer.Status.PENDING
        assert transfer.amount_minor == self.charge.amount_minor
        assert transfer.currency == self.charge.currency
        assert ProviderCallbackReceipt.objects.count() == 1

    def test_callback_rejects_invalid_or_expired_signatures_before_persisting_a_receipt(
        self,
    ) -> None:
        raw_body = self.callback_body()

        with pytest.raises(CallbackSignatureError):
            record_sandbox_pix_callback(
                raw_body=raw_body,
                signature="0" * 64,
                timestamp=CALLBACK_TIMESTAMP,
                signing_secret=SIGNING_SECRET,
                now=CALLBACK_TIME,
            )
        with pytest.raises(CallbackTimestampExpired):
            record_sandbox_pix_callback(
                raw_body=raw_body,
                signature=sign_sandbox_callback(SIGNING_SECRET, CALLBACK_TIMESTAMP, raw_body),
                timestamp=CALLBACK_TIMESTAMP,
                signing_secret=SIGNING_SECRET,
                now=datetime.fromtimestamp(1_720_000_301, tz=UTC),
            )

        assert ProviderCallbackReceipt.objects.count() == 0
        assert Transfer.objects.count() == 0
