"""Bearer API-key authentication without query-string or log exposure."""

from __future__ import annotations

import hmac
from dataclasses import dataclass

import redis
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from escrow.http import error_response
from escrow.integrations.key_service import fingerprint
from escrow.integrations.models import ApiKey
from escrow.integrations.rate_limit import RateLimitDecision, check_api_key_rate_limit


@dataclass(frozen=True)
class ApiKeyAuthentication:
    api_key: ApiKey


def _raw_bearer_secret(request: HttpRequest) -> str | None:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return None
    raw_secret = authorization.removeprefix("Bearer ").strip()
    if not raw_secret.startswith("esk_"):
        return None
    return raw_secret


def _prefix(raw_secret: str) -> str | None:
    parts = raw_secret.split("_", 2)
    if len(parts) != 3 or parts[0] != "esk" or not parts[1] or not parts[2]:
        return None
    return parts[1]


def _request_ip(request: HttpRequest) -> str | None:
    remote_address = request.META.get("REMOTE_ADDR")
    return remote_address if isinstance(remote_address, str) else None


def authenticate_api_key(
    request: HttpRequest,
    *,
    required_scope: str,
) -> ApiKeyAuthentication | HttpResponse:
    """Authenticate a bearer credential, then enforce scope and Redis quota."""
    raw_secret = _raw_bearer_secret(request)
    if raw_secret is None:
        return error_response("api_key_required", 401)
    prefix = _prefix(raw_secret)
    if prefix is None:
        return error_response("api_key_invalid", 401)
    api_key = ApiKey.objects.select_related("organization").filter(prefix=prefix).first()
    if api_key is None or not hmac.compare_digest(api_key.secret_hash, fingerprint(raw_secret)):
        return error_response("api_key_invalid", 401)
    if api_key.status != "ACTIVE" or not api_key.organization.is_active:
        return error_response("api_key_invalid", 401)
    try:
        decision: RateLimitDecision = check_api_key_rate_limit(str(api_key.id))
    except redis.RedisError:
        return error_response("api_key_rate_limit_unavailable", 503)
    if not decision.allowed:
        response = error_response("api_key_rate_limited", 429)
        response["Retry-After"] = str(decision.retry_after_seconds)
        return response
    if required_scope not in api_key.scopes:
        return error_response("api_key_scope_forbidden", 403)
    api_key.last_used_at = timezone.now()
    api_key.last_used_ip = _request_ip(request)
    api_key.save(update_fields=["last_used_at", "last_used_ip", "updated_at"])
    return ApiKeyAuthentication(api_key=api_key)
