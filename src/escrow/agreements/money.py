"""Strict decimal-string money conversion at the external API boundary."""

from __future__ import annotations

import re

SUPPORTED_CURRENCIES = frozenset({"BRL", "USD"})
MAX_MINOR_AMOUNT = 9_223_372_036_854_775_807
_DECIMAL_AMOUNT = re.compile(r"^(0|[1-9][0-9]*)(?:\.([0-9]{1,2}))?$")


class MoneyValidationError(ValueError):
    """Raised when a public money representation is ambiguous or unsupported."""


def parse_minor_amount(amount: object, currency: object) -> tuple[int, str]:
    """Accept a positive two-decimal string and return its integer minor units."""
    if not isinstance(currency, str) or currency not in SUPPORTED_CURRENCIES:
        raise MoneyValidationError("unsupported currency")
    if not isinstance(amount, str):
        raise MoneyValidationError("amount must be a decimal string")
    match = _DECIMAL_AMOUNT.fullmatch(amount)
    if match is None:
        raise MoneyValidationError("amount must have at most two decimal places")
    fractional = (match.group(2) or "").ljust(2, "0")
    minor = int(amount.split(".", 1)[0]) * 100 + int(fractional)
    if not 0 < minor <= MAX_MINOR_AMOUNT:
        raise MoneyValidationError("amount must be positive")
    return minor, currency


def format_minor_amount(amount_minor: int) -> str:
    """Render a stored minor-unit amount without floating point conversion."""
    return f"{amount_minor // 100}.{amount_minor % 100:02d}"
