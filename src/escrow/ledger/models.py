"""Append-only, double-entry accounting records."""

from __future__ import annotations

import uuid
from typing import Any

from django.db import models


class LedgerImmutableError(RuntimeError):
    """A posted ledger record was asked to change history."""


class AppendOnlyLedgerQuerySet(models.QuerySet["AppendOnlyLedgerRecord"]):
    """Make accidental ORM bulk mutations fail before reaching the database."""

    def delete(self) -> tuple[int, dict[str, int]]:
        raise LedgerImmutableError("ledger history is append-only")

    def update(self, **kwargs: Any) -> int:
        del kwargs
        raise LedgerImmutableError("ledger history is append-only")


class AppendOnlyLedgerManager(
    models.Manager.from_queryset(AppendOnlyLedgerQuerySet)  # type: ignore[misc]
):
    pass


class AppendOnlyLedgerRecord(models.Model):
    """SQLite-compatible guard; PostgreSQL adds database triggers in the migration."""

    objects = AppendOnlyLedgerManager()

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding or (
            self.pk is not None and type(self).objects.filter(pk=self.pk).exists()
        ):
            raise LedgerImmutableError("ledger history is append-only")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        del args, kwargs
        raise LedgerImmutableError("ledger history is append-only")


class ChartOfAccount(models.Model):
    """Small, system-owned chart used by every posting in the MVP."""

    class Code(models.TextChoices):
        PIX_CLEARING = "PIX_CLEARING", "PIX clearing"
        FUNDS_PENDING_RISK = "FUNDS_PENDING_RISK", "Funds pending risk"
        ESCROW_LIABILITY = "ESCROW_LIABILITY", "Escrow liability"
        ORGANIZATION_PAYABLE = "ORGANIZATION_PAYABLE", "Organization payable"
        PLATFORM_FEE_REVENUE = "PLATFORM_FEE_REVENUE", "Platform fee revenue"

    class AccountType(models.TextChoices):
        ASSET = "ASSET", "Asset"
        LIABILITY = "LIABILITY", "Liability"
        REVENUE = "REVENUE", "Revenue"

    class NormalSide(models.TextChoices):
        DEBIT = "DEBIT", "Debit"
        CREDIT = "CREDIT", "Credit"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=64, choices=Code.choices, unique=True)
    name = models.CharField(max_length=160)
    account_type = models.CharField(max_length=16, choices=AccountType.choices)
    normal_side = models.CharField(max_length=6, choices=NormalSide.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]


class LedgerTransaction(AppendOnlyLedgerRecord):
    """One financial posting, linked to its originating transfer and message key."""

    class Currency(models.TextChoices):
        BRL = "BRL", "Brazilian real"
        USD = "USD", "United States dollar"

    class Kind(models.TextChoices):
        FUNDING_RECEIVED = "FUNDING_RECEIVED", "Funding received"
        FUNDS_HELD = "FUNDS_HELD", "Funds held"
        FUNDING_REJECTED = "FUNDING_REJECTED", "Funding rejected"
        FUNDS_RELEASED = "FUNDS_RELEASED", "Funds released"
        FUNDS_REFUNDED = "FUNDS_REFUNDED", "Funds refunded"
        REVERSAL = "REVERSAL", "Reversal"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transfer = models.ForeignKey(
        "payments.Transfer",
        on_delete=models.PROTECT,
        related_name="ledger_transactions",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    idempotency_key = models.CharField(max_length=255, unique=True)
    posting_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(currency__in=["BRL", "USD"]),
                name="ledger_transaction_currency_is_brl_or_usd",
            ),
            models.CheckConstraint(
                condition=~models.Q(idempotency_key=""),
                name="ledger_transaction_idempotency_key_not_empty",
            ),
            models.UniqueConstraint(
                fields=["transfer", "kind"],
                name="ledger_transaction_transfer_kind_unique",
            ),
        ]


class LedgerEntry(AppendOnlyLedgerRecord):
    """Exactly one debit or credit minor-unit amount in a ledger transaction."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ledger_transaction = models.ForeignKey(
        LedgerTransaction,
        on_delete=models.PROTECT,
        related_name="entries",
    )
    account = models.ForeignKey(
        ChartOfAccount,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
    )
    currency = models.CharField(max_length=3, choices=LedgerTransaction.Currency.choices)
    debit_minor = models.PositiveBigIntegerField(default=0)
    credit_minor = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(debit_minor__gt=0) & models.Q(credit_minor=0))
                | (models.Q(debit_minor=0) & models.Q(credit_minor__gt=0)),
                name="ledger_entry_exactly_one_side_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(currency__in=["BRL", "USD"]),
                name="ledger_entry_currency_is_brl_or_usd",
            ),
        ]
        indexes = [
            models.Index(
                fields=["ledger_transaction", "currency"],
                name="ledger_ledg_ledger__ddd13d_idx",
            )
        ]
