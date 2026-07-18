from __future__ import annotations

import uuid

from django.test import TestCase

from escrow.agreements.models import EscrowAgreement
from escrow.ledger.models import (
    ChartOfAccount,
    LedgerEntry,
    LedgerImmutableError,
    LedgerTransaction,
)
from escrow.ledger.services import (
    LedgerEntryInput,
    LedgerIdempotencyConflict,
    LedgerPosting,
    LedgerPostingValidationError,
    post_ledger_transaction,
)
from escrow.organizations.models import Organization
from escrow.payments.models import Transfer


class LedgerPostingTests(TestCase):
    def setUp(self) -> None:
        organization = Organization.objects.create(name="Ledger test organization")
        agreement = EscrowAgreement.objects.create(
            organization=organization,
            external_customer_id="customer-ledger-test",
            customer_name_masked="A*** S***",
            customer_email_masked="a***@example.test",
            customer_document_masked="***.***.***-25",
            customer_document_kind="CPF",
            customer_email_blind_index="0" * 64,
            customer_document_blind_index="1" * 64,
            customer_pii_ciphertext=b"test-ciphertext",
            customer_pii_nonce=b"test-nonce",
            customer_pii_encrypted_data_key=b"test-data-key",
            customer_pii_kms_key_id="test-key",
            checkout_token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            amount_minor=5_000_000,
            currency=EscrowAgreement.Currency.BRL,
            fee_bps=200,
            delivery_window_days=7,
        )
        self.transfer = Transfer.objects.create(
            agreement=agreement,
            kind=Transfer.Kind.FUNDING,
            amount_minor=agreement.amount_minor,
            currency=agreement.currency,
            provider=Transfer.Provider.SANDBOX_PIX,
            provider_reference="pix-ledger-test",
            idempotency_key="transfer-ledger-test",
        )

    def posting(
        self,
        *,
        idempotency_key: str = "ledger-funding-confirmed-001",
        entries: tuple[LedgerEntryInput, ...] | None = None,
    ) -> LedgerPosting:
        return LedgerPosting(
            transfer_id=self.transfer.id,
            kind=LedgerTransaction.Kind.FUNDING_RECEIVED,
            currency="BRL",
            idempotency_key=idempotency_key,
            entries=entries
            or (
                LedgerEntryInput.debit("PIX_CLEARING", 5_000_000, "BRL"),
                LedgerEntryInput.credit("FUNDS_PENDING_RISK", 5_000_000, "BRL"),
            ),
        )

    def test_posts_a_balanced_transaction_in_minor_units(self) -> None:
        result = post_ledger_transaction(self.posting())

        assert result.replayed is False
        assert result.transaction.transfer_id == self.transfer.id
        assert ChartOfAccount.objects.filter(code="PIX_CLEARING").exists()
        entries = list(result.transaction.entries.order_by("id"))
        assert len(entries) == 2
        assert sum(entry.debit_minor for entry in entries) == 5_000_000
        assert sum(entry.credit_minor for entry in entries) == 5_000_000
        assert {entry.currency for entry in entries} == {"BRL"}

    def test_rejects_an_unbalanced_posting_before_it_is_persisted(self) -> None:
        posting = self.posting(
            entries=(
                LedgerEntryInput.debit("PIX_CLEARING", 5_000_000, "BRL"),
                LedgerEntryInput.credit("FUNDS_PENDING_RISK", 4_999_999, "BRL"),
            )
        )

        with self.assertRaises(LedgerPostingValidationError):
            post_ledger_transaction(posting)

        assert LedgerTransaction.objects.count() == 0
        assert LedgerEntry.objects.count() == 0

    def test_rejects_entries_from_a_different_currency(self) -> None:
        posting = self.posting(
            entries=(
                LedgerEntryInput.debit("PIX_CLEARING", 5_000_000, "BRL"),
                LedgerEntryInput.credit("FUNDS_PENDING_RISK", 5_000_000, "USD"),
            )
        )

        with self.assertRaises(LedgerPostingValidationError):
            post_ledger_transaction(posting)

        assert LedgerTransaction.objects.count() == 0

    def test_rejects_a_balanced_amount_that_does_not_match_its_transfer(self) -> None:
        posting = self.posting(
            entries=(
                LedgerEntryInput.debit("PIX_CLEARING", 4_999_999, "BRL"),
                LedgerEntryInput.credit("FUNDS_PENDING_RISK", 4_999_999, "BRL"),
            )
        )

        with self.assertRaises(LedgerPostingValidationError):
            post_ledger_transaction(posting)

        assert LedgerTransaction.objects.count() == 0

    def test_replays_the_same_idempotency_key_without_a_second_financial_effect(self) -> None:
        first = post_ledger_transaction(self.posting())
        replay = post_ledger_transaction(self.posting())

        assert first.replayed is False
        assert replay.replayed is True
        assert replay.transaction.id == first.transaction.id
        assert LedgerTransaction.objects.count() == 1
        assert LedgerEntry.objects.count() == 2

    def test_rejects_a_changed_posting_for_an_existing_idempotency_key(self) -> None:
        post_ledger_transaction(self.posting())
        changed = self.posting(
            entries=(
                LedgerEntryInput.debit("PIX_CLEARING", 5_000_000, "BRL"),
                LedgerEntryInput.credit("FUNDS_PENDING_RISK", 4_900_000, "BRL"),
                LedgerEntryInput.credit("ESCROW_LIABILITY", 100_000, "BRL"),
            )
        )

        with self.assertRaises(LedgerIdempotencyConflict):
            post_ledger_transaction(changed)

        assert LedgerTransaction.objects.count() == 1
        assert LedgerEntry.objects.count() == 2

    def test_posted_ledger_history_cannot_be_mutated_or_deleted(self) -> None:
        transaction = post_ledger_transaction(self.posting()).transaction
        entry = transaction.entries.first()
        assert entry is not None

        transaction.kind = LedgerTransaction.Kind.FUNDS_HELD
        with self.assertRaises(LedgerImmutableError):
            transaction.save()
        with self.assertRaises(LedgerImmutableError):
            entry.delete()
        with self.assertRaises(LedgerImmutableError):
            LedgerEntry.objects.filter(id=entry.id).update(debit_minor=1)

        assert LedgerTransaction.objects.get(id=transaction.id).kind == (
            LedgerTransaction.Kind.FUNDING_RECEIVED
        )
        assert LedgerEntry.objects.filter(ledger_transaction=transaction).count() == 2
