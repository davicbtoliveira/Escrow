from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from escrow.agreements.lifecycle import (
    AgreementStateConflict,
    confirm_funding,
    mark_funds_held,
    start_funding,
)
from escrow.agreements.models import EscrowAgreement
from escrow.organizations.models import Organization


def agreement() -> EscrowAgreement:
    organization = Organization.objects.create(name="Lifecycle Organization")
    return EscrowAgreement.objects.create(
        organization=organization,
        external_customer_id="lifecycle-buyer",
        customer_name_masked="A***",
        customer_email_masked="a***@example.test",
        customer_document_masked="***.***.***-25",
        customer_document_kind="CPF",
        customer_email_blind_index="a" * 64,
        customer_document_blind_index="b" * 64,
        customer_pii_ciphertext=b"ciphertext",
        customer_pii_nonce=b"nonce",
        customer_pii_encrypted_data_key=b"encrypted-key",
        customer_pii_kms_key_id="test-key",
        checkout_token_hash="c" * 64,
        amount_minor=50_000,
        currency="BRL",
        fee_bps=200,
        delivery_window_days=7,
    )


class FundingLifecycleTests(TestCase):
    def test_payment_processing_and_confirmation_increment_safe_realtime_sequence(self) -> None:
        escrow_agreement = agreement()
        confirmed_at = timezone.now()

        processing = start_funding(escrow_agreement.id)
        confirmed = confirm_funding(escrow_agreement.id, confirmed_at=confirmed_at)
        held = mark_funds_held(escrow_agreement.id)

        assert processing.status == EscrowAgreement.Status.FUNDING_PROCESSING
        assert processing.version == 1
        assert processing.realtime_sequence == 1
        assert confirmed.funding_confirmed_at == confirmed_at
        assert confirmed.delivery_due_at == confirmed_at + timedelta(days=7)
        assert confirmed.version == 2
        assert confirmed.realtime_sequence == 2
        assert held.status == EscrowAgreement.Status.HELD
        assert held.version == 3
        assert held.realtime_sequence == 3

    def test_confirmation_cannot_skip_or_repeat_the_state_machine(self) -> None:
        escrow_agreement = agreement()

        with self.assertRaises(AgreementStateConflict):
            confirm_funding(escrow_agreement.id, confirmed_at=timezone.now())

        start_funding(escrow_agreement.id)
        confirm_funding(escrow_agreement.id, confirmed_at=timezone.now())
        with self.assertRaises(AgreementStateConflict):
            confirm_funding(escrow_agreement.id, confirmed_at=timezone.now())
