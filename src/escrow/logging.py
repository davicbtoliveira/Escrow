"""Safe JSON logging for local operational traces."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    """Emit known-safe structured fields and never serialize request objects."""

    safe_extra_fields = (
        "correlation_id",
        "causation_id",
        "event_id",
        "organization_id",
        "agreement_id",
        "transaction_id",
        "http_method",
        "http_path",
        "http_status",
        "duration_ms",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for field in self.safe_extra_fields:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
