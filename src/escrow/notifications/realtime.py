"""Safe, small status snapshots for public checkout realtime delivery."""

from __future__ import annotations

from typing import Final, TypedDict
from uuid import UUID

from escrow.agreements.models import EscrowAgreement

AGREEMENT_STATUS_CHANGED_TYPE: Final = "AgreementStatusChanged.v1"
AGREEMENT_STATUS_GROUP_PREFIX: Final = "agreement-status."
_STATUS_FIELDS: Final = frozenset({"agreement_id", "status", "sequence"})
_MAX_SEQUENCE: Final = 9_223_372_036_854_775_807


class RealtimeStatusValidationError(ValueError):
    """A realtime event is not a safe public checkout status snapshot."""


class PublicStatusSnapshot(TypedDict):
    agreement_id: str
    status: str
    sequence: int


def agreement_status_group_name(agreement_id: UUID | str) -> str:
    """Build the stable, capability-free group name for one agreement."""
    return f"{AGREEMENT_STATUS_GROUP_PREFIX}{_normalized_agreement_id(agreement_id)}"


def public_status_snapshot(payload: object) -> PublicStatusSnapshot:
    """Whitelist the only values that may cross the realtime boundary."""
    if not isinstance(payload, dict) or set(payload) != _STATUS_FIELDS:
        raise RealtimeStatusValidationError("status payload has an unsupported shape")
    agreement_id = _normalized_agreement_id(payload["agreement_id"])
    status = payload["status"]
    sequence = payload["sequence"]
    if not isinstance(status, str) or status not in EscrowAgreement.Status.values:
        raise RealtimeStatusValidationError("status payload has an invalid agreement status")
    if type(sequence) is not int or not 1 <= sequence <= _MAX_SEQUENCE:
        raise RealtimeStatusValidationError("status payload has an invalid sequence")
    return {
        "agreement_id": agreement_id,
        "status": status,
        "sequence": sequence,
    }


def channels_status_event(snapshot: PublicStatusSnapshot) -> dict[str, str | int]:
    """Add Channels' dispatch key without forwarding envelope metadata."""
    return {
        "type": "agreement.status",
        "agreement_id": snapshot["agreement_id"],
        "status": snapshot["status"],
        "sequence": snapshot["sequence"],
    }


def _normalized_agreement_id(value: object) -> str:
    try:
        return str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise RealtimeStatusValidationError("status payload has an invalid agreement ID") from error
