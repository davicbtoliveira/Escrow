from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from escrow.agreements.models import EscrowAgreement
from escrow.organizations.models import Organization
from escrow.payments.models import Transfer


def agreement() -> EscrowAgreement:
    organization = Organization.objects.create(name="Loja de Teste")
    return EscrowAgreement.objects.create(
        organization=organization,
        external_customer_id="buyer-001",
        customer_name_masked="A***",
        customer_email_masked="a***@example.test",
        customer_document_masked="***.***.***-25",
        customer_document_kind="CPF",
        customer_email_blind_index="a" * 64,
        customer_document_blind_index="b" * 64,
        customer_pii_ciphertext=b"ciphertext",
        customer_pii_nonce=b"nonce",
        customer_pii_encrypted_data_key=b"encrypted-key",
        customer_pii_kms_key_id="local-test-key",
        checkout_token_hash="c" * 64,
        amount_minor=50_000,
        currency="BRL",
        fee_bps=200,
        delivery_window_days=7,
    )


class TransferDatabaseInvariantTests(TestCase):
    def test_only_one_financial_intent_of_each_kind_can_exist_per_agreement(self) -> None:
        escrow_agreement = agreement()
        Transfer.objects.create(
            agreement=escrow_agreement,
            kind=Transfer.Kind.FUNDING,
            amount_minor=50_000,
            currency="BRL",
            provider=Transfer.Provider.SANDBOX_PIX,
            provider_reference="pix_first",
            idempotency_key="funding-001",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Transfer.objects.create(
                agreement=escrow_agreement,
                kind=Transfer.Kind.FUNDING,
                amount_minor=50_000,
                currency="BRL",
                provider=Transfer.Provider.SANDBOX_PIX,
                provider_reference="pix_second",
                idempotency_key="funding-002",
            )

    def test_transfer_requires_positive_minor_amount_and_a_mvp_currency(self) -> None:
        escrow_agreement = agreement()

        with self.assertRaises(IntegrityError), transaction.atomic():
            Transfer.objects.create(
                agreement=escrow_agreement,
                kind=Transfer.Kind.FUNDING,
                amount_minor=0,
                currency="BRL",
                provider=Transfer.Provider.SANDBOX_PIX,
                provider_reference="pix_zero",
                idempotency_key="funding-zero-001",
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Transfer.objects.create(
                agreement=escrow_agreement,
                kind=Transfer.Kind.FUNDING,
                amount_minor=50_000,
                currency="EUR",
                provider=Transfer.Provider.SANDBOX_PIX,
                provider_reference="pix-eur",
                idempotency_key="funding-eur-001",
            )

    def test_transfer_idempotency_is_scoped_to_its_agreement_and_protects_it(self) -> None:
        escrow_agreement = agreement()
        Transfer.objects.create(
            agreement=escrow_agreement,
            kind=Transfer.Kind.FUNDING,
            amount_minor=50_000,
            currency="BRL",
            provider=Transfer.Provider.SANDBOX_PIX,
            provider_reference="pix-original",
            idempotency_key="same-intent-key",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Transfer.objects.create(
                agreement=escrow_agreement,
                kind=Transfer.Kind.RELEASE,
                amount_minor=50_000,
                currency="BRL",
                provider=Transfer.Provider.INTERNAL,
                provider_reference="release-internal",
                idempotency_key="same-intent-key",
            )
        with self.assertRaises(ProtectedError):
            escrow_agreement.delete()
