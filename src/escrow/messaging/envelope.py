"""The versioned JSON message contract from ADR 0005."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID


class EnvelopeValidationError(ValueError):
    """A broker message does not conform to the supported envelope contract."""


_ENVELOPE_FIELDS = frozenset(
    {
        "message_id",
        "type",
        "version",
        "occurred_at",
        "correlation_id",
        "causation_id",
        "tenant_id",
        "payload",
    }
)


@dataclass(frozen=True)
class MessageEnvelope:
    """An immutable, transport-independent versioned message."""

    message_id: UUID
    message_type: str
    version: int
    occurred_at: datetime
    correlation_id: str
    causation_id: str | None
    tenant_id: str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.message_id, UUID):
            raise EnvelopeValidationError("message_id must be a UUID")
        if type(self.version) is not int or self.version < 1:
            raise EnvelopeValidationError("version must be a positive integer")
        if not isinstance(self.message_type, str) or not self.message_type.endswith(
            f".v{self.version}"
        ):
            raise EnvelopeValidationError("type must end with its version suffix")
        if not isinstance(self.occurred_at, datetime) or self.occurred_at.tzinfo is None:
            raise EnvelopeValidationError("occurred_at must be timezone-aware")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))
        _validate_identifier("correlation_id", self.correlation_id)
        if self.causation_id is not None:
            _validate_identifier("causation_id", self.causation_id)
        _validate_identifier("tenant_id", self.tenant_id)
        if not isinstance(self.payload, dict):
            raise EnvelopeValidationError("payload must be a JSON object")
        try:
            json.dumps(self.payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError) as error:
            raise EnvelopeValidationError("payload must be JSON serializable") from error

    @classmethod
    def build(
        cls,
        *,
        message_id: UUID,
        message_type: str,
        version: int,
        occurred_at: datetime,
        correlation_id: str,
        causation_id: str | None,
        tenant_id: str,
        payload: dict[str, Any],
    ) -> MessageEnvelope:
        """Build an envelope while keeping the wire name ``type`` out of Python syntax."""
        return cls(
            message_id=message_id,
            message_type=message_type,
            version=version,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            causation_id=causation_id,
            tenant_id=tenant_id,
            payload=payload,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the exact JSON object accepted by every queue consumer."""
        return {
            "message_id": str(self.message_id),
            "type": self.message_type,
            "version": self.version,
            "occurred_at": self.occurred_at.isoformat().replace("+00:00", "Z"),
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "tenant_id": self.tenant_id,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        """Serialize with JSON only; pickle is intentionally never an option."""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | bytes) -> MessageEnvelope:
        """Parse a broker body without accepting malformed or surprise fields."""
        try:
            decoded = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            value = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EnvelopeValidationError("message is not valid JSON") from error
        return cls.from_dict(value)

    @classmethod
    def from_dict(cls, value: object) -> MessageEnvelope:
        """Parse the strict ADR 0005 envelope shape."""
        if not isinstance(value, Mapping) or set(value) != _ENVELOPE_FIELDS:
            raise EnvelopeValidationError("message has an unsupported envelope shape")
        try:
            message_id = UUID(str(value["message_id"]))
            occurred_at = _parse_occurred_at(value["occurred_at"])
        except (TypeError, ValueError) as error:
            raise EnvelopeValidationError("message has invalid identifiers or timestamp") from error
        return cls.build(
            message_id=message_id,
            message_type=value["type"],
            version=value["version"],
            occurred_at=occurred_at,
            correlation_id=value["correlation_id"],
            causation_id=value["causation_id"],
            tenant_id=value["tenant_id"],
            payload=value["payload"],
        )


def _parse_occurred_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("occurred_at must be a string")
    occurred_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if occurred_at.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware")
    return occurred_at


def _validate_identifier(name: str, value: object) -> None:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise EnvelopeValidationError(f"{name} must be a non-empty string up to 128 characters")
