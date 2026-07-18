"""The narrow application seam for balanced, idempotent ledger postings."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Self
from uuid import UUID

from django.db import IntegrityError, transaction

from escrow.ledger.models import ChartOfAccount, LedgerEntry, LedgerTransaction
from escrow.payments.models import Transfer

_MAX_MINOR_UNITS = 9_223_372_036_854_775_807


class LedgerPostingValidationError(ValueError):
    """A caller attempted an invalid accounting command."""


class LedgerIdempotencyConflict(RuntimeError):
    """One idempotency capability was reused for a different posting."""


class LedgerFinancialIntentAlreadyPosted(RuntimeError):
    """A one-off transfer posting already has a different idempotency key."""


@dataclass(frozen=True, slots=True)
class LedgerEntryInput:
    account_code: str
    debit_minor: int
    credit_minor: int
    currency: str

    @classmethod
    def debit(cls, account_code: str, amount_minor: int, currency: str) -> Self:
        return cls(
            account_code=account_code,
            debit_minor=amount_minor,
            credit_minor=0,
            currency=currency,
        )

    @classmethod
    def credit(cls, account_code: str, amount_minor: int, currency: str) -> Self:
        return cls(
            account_code=account_code,
            debit_minor=0,
            credit_minor=amount_minor,
            currency=currency,
        )


@dataclass(frozen=True, slots=True)
class LedgerPosting:
    transfer_id: UUID
    kind: str
    currency: str
    idempotency_key: str
    entries: tuple[LedgerEntryInput, ...]


@dataclass(frozen=True, slots=True)
class LedgerPostingResult:
    transaction: LedgerTransaction
    replayed: bool


def post_ledger_transaction(posting: LedgerPosting) -> LedgerPostingResult:
    """Atomically post a validated double-entry transaction or replay it once."""
    _validate_posting(posting)
    posting_hash = _posting_hash(posting)
    try:
        with transaction.atomic():
            transfer = Transfer.objects.select_for_update().filter(id=posting.transfer_id).first()
            if transfer is None or transfer.currency != posting.currency:
                raise LedgerPostingValidationError("posting currency does not match its transfer")
            if _posting_amount(posting) != transfer.amount_minor:
                raise LedgerPostingValidationError("posting amount does not match its transfer")

            existing = (
                LedgerTransaction.objects.select_for_update()
                .filter(idempotency_key=posting.idempotency_key)
                .first()
            )
            if existing is not None:
                return _replay_or_conflict(existing, posting_hash)

            intent = (
                LedgerTransaction.objects.select_for_update()
                .filter(transfer_id=posting.transfer_id, kind=posting.kind)
                .first()
            )
            if intent is not None:
                if intent.posting_hash == posting_hash:
                    return LedgerPostingResult(transaction=intent, replayed=True)
                raise LedgerFinancialIntentAlreadyPosted

            accounts = _accounts_for(posting.entries)
            ledger_transaction = LedgerTransaction.objects.create(
                transfer=transfer,
                kind=posting.kind,
                currency=posting.currency,
                idempotency_key=posting.idempotency_key,
                posting_hash=posting_hash,
            )
            LedgerEntry.objects.bulk_create(
                [
                    LedgerEntry(
                        ledger_transaction=ledger_transaction,
                        account=accounts[entry.account_code],
                        currency=entry.currency,
                        debit_minor=entry.debit_minor,
                        credit_minor=entry.credit_minor,
                    )
                    for entry in posting.entries
                ]
            )
            return LedgerPostingResult(transaction=ledger_transaction, replayed=False)
    except IntegrityError as error:
        return _recover_duplicate(posting, posting_hash, error)


def _validate_posting(posting: LedgerPosting) -> None:
    if not isinstance(posting.transfer_id, UUID):
        raise LedgerPostingValidationError("transfer id is invalid")
    if posting.kind not in LedgerTransaction.Kind.values:
        raise LedgerPostingValidationError("ledger transaction kind is invalid")
    if posting.currency not in LedgerTransaction.Currency.values:
        raise LedgerPostingValidationError("ledger transaction currency is invalid")
    if not isinstance(posting.idempotency_key, str) or not posting.idempotency_key.strip():
        raise LedgerPostingValidationError("idempotency key is invalid")
    if len(posting.idempotency_key) > 255 or len(posting.entries) < 2:
        raise LedgerPostingValidationError("ledger posting is invalid")

    debit_total = 0
    credit_total = 0
    for entry in posting.entries:
        if not isinstance(entry.account_code, str) or not entry.account_code:
            raise LedgerPostingValidationError("chart account is invalid")
        if entry.currency != posting.currency:
            raise LedgerPostingValidationError("cross-currency ledger postings are not supported")
        if type(entry.debit_minor) is not int or type(entry.credit_minor) is not int:
            raise LedgerPostingValidationError("ledger amounts must be integer minor units")
        if not (0 < entry.debit_minor <= _MAX_MINOR_UNITS and entry.credit_minor == 0) and not (
            0 < entry.credit_minor <= _MAX_MINOR_UNITS and entry.debit_minor == 0
        ):
            raise LedgerPostingValidationError("each ledger entry needs exactly one positive side")
        debit_total += entry.debit_minor
        credit_total += entry.credit_minor
    if debit_total != credit_total:
        raise LedgerPostingValidationError("ledger posting is not balanced")


def _accounts_for(entries: tuple[LedgerEntryInput, ...]) -> dict[str, ChartOfAccount]:
    codes = {entry.account_code for entry in entries}
    accounts = {account.code: account for account in ChartOfAccount.objects.filter(code__in=codes)}
    if len(accounts) != len(codes):
        raise LedgerPostingValidationError("chart account does not exist")
    return accounts


def _posting_amount(posting: LedgerPosting) -> int:
    return sum(entry.debit_minor for entry in posting.entries)


def _posting_hash(posting: LedgerPosting) -> str:
    entries = sorted(
        (
            entry.account_code,
            entry.debit_minor,
            entry.credit_minor,
            entry.currency,
        )
        for entry in posting.entries
    )
    encoded = json.dumps(
        {
            "transfer_id": str(posting.transfer_id),
            "kind": posting.kind,
            "currency": posting.currency,
            "entries": entries,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _replay_or_conflict(
    existing: LedgerTransaction,
    posting_hash: str,
) -> LedgerPostingResult:
    if existing.posting_hash != posting_hash:
        raise LedgerIdempotencyConflict
    return LedgerPostingResult(transaction=existing, replayed=True)


def _recover_duplicate(
    posting: LedgerPosting,
    posting_hash: str,
    error: IntegrityError,
) -> LedgerPostingResult:
    """Recover a concurrent unique-key race without creating another effect."""
    existing = LedgerTransaction.objects.filter(idempotency_key=posting.idempotency_key).first()
    if existing is not None:
        return _replay_or_conflict(existing, posting_hash)
    intent = LedgerTransaction.objects.filter(
        transfer_id=posting.transfer_id,
        kind=posting.kind,
    ).first()
    if intent is not None:
        if intent.posting_hash == posting_hash:
            return LedgerPostingResult(transaction=intent, replayed=True)
        raise LedgerFinancialIntentAlreadyPosted
    raise error
