"""Small HTTP primitives shared by explicit API views."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import wraps
from typing import Any, cast

from django.http import HttpRequest, HttpResponse, JsonResponse


class InvalidJsonBody(ValueError):
    """Raised when a request body is not a JSON object."""


def parse_json_body(request: HttpRequest) -> dict[str, Any]:
    """Accept only a JSON object so API commands have a stable shape."""
    try:
        payload: object = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidJsonBody from error
    if not isinstance(payload, dict):
        raise InvalidJsonBody
    return cast(dict[str, Any], payload)


def error_response(
    code: str,
    status: int,
    *,
    errors: dict[str, list[str]] | None = None,
) -> JsonResponse:
    """Return the stable, non-sensitive error envelope used by the B2B API."""
    payload: dict[str, object] = {"code": code}
    if errors is not None:
        payload["errors"] = errors
    return JsonResponse(payload, status=status)


def session_required(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    """Require a Django session without redirecting an API consumer to HTML."""

    @wraps(view)
    def wrapped(request: HttpRequest, *args: object, **kwargs: object) -> HttpResponse:
        if not request.user.is_authenticated:
            return error_response("authentication_required", 401)
        return view(request, *args, **kwargs)

    return wrapped


def csrf_failure(_: HttpRequest, reason: str = "") -> JsonResponse:
    """Avoid exposing CSRF internals while preserving a machine-readable response."""
    del reason
    return error_response("csrf_failed", 403)
