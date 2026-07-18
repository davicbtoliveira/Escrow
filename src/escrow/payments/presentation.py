"""Safe public projections of sandbox payment state."""

from __future__ import annotations

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.money import format_minor_amount
from escrow.payments.models import SandboxPixCharge


def public_payment_payload(charge: SandboxPixCharge) -> dict[str, str]:
    """Render only the fake payment instruction safe for a checkout bearer link."""
    return {
        "id": str(charge.id),
        "status": charge.status,
        "amount": format_minor_amount(charge.amount_minor),
        "currency": charge.currency,
        "pix_copy_paste": f"ESCROW-SANDBOX-PIX:{charge.provider_reference}",
    }


def public_payment_for_agreement(agreement: EscrowAgreement) -> dict[str, str] | None:
    """Return the existing public PIX projection without revealing customer identity."""
    charge = SandboxPixCharge.objects.filter(agreement=agreement).first()
    return None if charge is None else public_payment_payload(charge)
