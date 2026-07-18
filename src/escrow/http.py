"""Small HTTP primitives shared by explicit API views."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import wraps
from typing import Any, cast

from django.http import HttpRequest, HttpResponse, JsonResponse

from escrow.correlation import get_correlation_id


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
    payload: dict[str, object] = {
        "code": code,
        "message": _ERROR_MESSAGES.get(code, "Não foi possível concluir a solicitação."),
        "details": errors or {},
        "correlation_id": get_correlation_id(),
    }
    if errors is not None:
        payload["errors"] = errors
    return JsonResponse(payload, status=status)


_ERROR_MESSAGES = {
    "authentication_required": "Autenticação obrigatória.",
    "api_key_required": "API key obrigatória.",
    "api_key_invalid": "API key inválida.",
    "api_key_scope_forbidden": "A API key não possui o escopo necessário.",
    "api_key_rate_limited": "Limite de requisições da API key atingido.",
    "api_key_rate_limit_unavailable": "O controle de limite está indisponível.",
    "method_not_allowed": "Método HTTP não permitido neste recurso.",
    "organization_role_forbidden": "Seu papel não permite esta ação.",
    "validation_error": "Existem campos inválidos.",
}


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


def drf_exception_handler(exc: Exception, context: dict[str, object]) -> object | None:
    """Translate DRF-generated errors into the same public contract as Django views."""
    from rest_framework.response import Response
    from rest_framework.views import exception_handler

    response = exception_handler(exc, context)
    if response is None:
        return None
    raw_details = response.data
    details = raw_details if isinstance(raw_details, dict) else {"detail": str(raw_details)}
    code = str(getattr(exc, "default_code", "api_error"))
    payload = {
        "code": code,
        "message": _ERROR_MESSAGES.get(code, "Não foi possível concluir a solicitação."),
        "details": details,
        "correlation_id": get_correlation_id(),
    }
    normalized = Response(payload, status=response.status_code)
    for header, value in response.items():
        normalized[header] = value
    return normalized
