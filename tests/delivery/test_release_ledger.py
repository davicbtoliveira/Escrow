from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from django.test import TestCase

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.money import calculate_release_fee_minor
from escrow.ledger.models import LedgerEntry, LedgerTransaction
from escrow.ledger.services import LedgerEntryInput, LedgerPosting, post_ledger_transaction
from escrow.ledger.tasks import release_funds
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.models import ProcessedMessage
from escrow.organizations.models import Organization
from escrow.payments.models import Transfer


class ReleaseLedgerTaskTests(TestCase):
    def setUp(self) -> None:
        organization = Organization.objects.create(name="Release ledger organization")
        self.agreement = EscrowAgreement.objects.create(
            organization=organization,
            external_customer_id="release-ledger-buyer",
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
            checkout_token_hash=f"checkout-{uuid4().hex}",
            amount_minor=50_000,
            currency="BRL",
            fee_bps=200,
            delivery_window_days=7,
            status=EscrowAgreement.Status.RELEASE_PENDING,
        )
        funding = Transfer.objects.create(
            agreement=self.agreement,
            kind=Transfer.Kind.FUNDING,
            status=Transfer.Status.COMPLETED,
            amount_minor=self.agreement.amount_minor,
            currency=self.agreement.currency,
            provider=Transfer.Provider.SANDBOX_PIX,
            provider_reference="release-ledger-funding",
            idempotency_key="release-ledger-funding",
        )
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=funding.id,
                kind=LedgerTransaction.Kind.FUNDS_HELD,
                currency="BRL",
                idempotency_key="release-ledger-held",
                entries=(
                    LedgerEntryInput.debit("FUNDS_PENDING_RISK", 50_000, "BRL"),
                    LedgerEntryInput.credit("ESCROW_LIABILITY", 50_000, "BRL"),
                ),
            )
        )
        self.release = Transfer.objects.create(
            agreement=self.agreement,
            kind=Transfer.Kind.RELEASE,
            amount_minor=self.agreement.amount_minor,
            currency=self.agreement.currency,
            provider=Transfer.Provider.INTERNAL,
            provider_reference="release-ledger-release",
            idempotency_key="release-ledger-release",
        )
        self.envelope = MessageEnvelope.build(
            message_id=uuid4(),
            message_type="ReleaseFunds.v1",
            version=1,
            occurred_at=datetime(2026, 7, 18, 12, tzinfo=UTC),
            correlation_id="release-ledger-correlation",
            causation_id="customer-acceptance-001",
            tenant_id=str(organization.id),
            payload={
                "agreement_id": str(self.agreement.id),
                "transfer_id": str(self.release.id),
            },
        )

    def test_release_posts_gross_fee_and_net_once_when_the_message_is_replayed(self) -> None:
        release_funds.apply(args=[self.envelope.to_dict()]).get()
        release_funds.apply(args=[self.envelope.to_dict()]).get()

        self.agreement.refresh_from_db()
        self.release.refresh_from_db()
        released = LedgerTransaction.objects.get(kind=LedgerTransaction.Kind.FUNDS_RELEASED)
        entries = LedgerEntry.objects.filter(ledger_transaction=released)

        assert self.agreement.status == EscrowAgreement.Status.RELEASED
        assert self.release.status == Transfer.Status.COMPLETED
        assert ProcessedMessage.objects.filter(message_id=self.envelope.message_id).count() == 1
        assert set(entries.values_list("account__code", "debit_minor", "credit_minor")) == {
            ("ESCROW_LIABILITY", 50_000, 0),
            ("ORGANIZATION_PAYABLE", 0, 49_000),
            ("PLATFORM_FEE_REVENUE", 0, 1_000),
        }
        assert sum(entry.debit_minor for entry in entries) == 50_000
        assert sum(entry.credit_minor for entry in entries) == 50_000
        released = LedgerTransaction.objects.filter(kind=LedgerTransaction.Kind.FUNDS_RELEASED)
        assert released.count() == 1

    def test_fee_uses_integer_round_half_up_without_float_conversion(self) -> None:
        assert calculate_release_fee_minor(25, 200) == 1
        assert calculate_release_fee_minor(24, 200) == 0
