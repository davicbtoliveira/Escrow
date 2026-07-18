"""Correlation identifiers for supportable, secret-safe API failures."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from time import perf_counter

from django.http import HttpRequest, HttpResponse

_CORRELATION_ID = ContextVar("correlation_id", default="")
_VALID_CORRELATION_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
logger = logging.getLogger("escrow.request")


def get_correlation_id() -> str:
    """Return the request correlation identifier, or create one outside HTTP."""
    value = _CORRELATION_ID.get()
    if value:
        return value
    return str(uuid.uuid4())


class CorrelationIdMiddleware:
    """Echo a validated correlation ID without retaining sensitive request data."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        requested = request.headers.get("X-Correlation-ID", "")
        correlation_id = (
            requested if _VALID_CORRELATION_ID.fullmatch(requested) else str(uuid.uuid4())
        )
        token = _CORRELATION_ID.set(correlation_id)
        started_at = perf_counter()
        try:
            response = self.get_response(request)
        except Exception:
            logger.error(
                "http_request_failed",
                extra={
                    "correlation_id": correlation_id,
                    "duration_ms": round((perf_counter() - started_at) * 1000, 2),
                    "http_method": request.method,
                    "http_path": request.path,
                },
            )
            raise
        else:
            logger.info(
                "http_request_completed",
                extra={
                    "correlation_id": correlation_id,
                    "duration_ms": round((perf_counter() - started_at) * 1000, 2),
                    "http_method": request.method,
                    "http_path": request.path,
                    "http_status": response.status_code,
                },
            )
            response["X-Correlation-ID"] = correlation_id
            return response
        finally:
            _CORRELATION_ID.reset(token)
