"""Session-managed API keys and a small bearer-authenticated integration surface."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from django.conf import settings
from django.db import IntegrityError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from escrow.audit.services import record_audit_event
from escrow.http import InvalidJsonBody, error_response, parse_json_body, session_required
from escrow.identity.models import User
from escrow.integrations.authentication import ApiKeyAuthentication, authenticate_api_key
from escrow.integrations.key_service import (
    VALID_API_KEY_SCOPES,
    ActiveApiKeyLimitError,
    create_api_key,
    revoke_api_key,
    rotate_api_key,
)
from escrow.integrations.models import ApiKey, WebhookDelivery, WebhookEndpoint
from escrow.integrations.webhooks import (
    WebhookEndpointValidationError,
    create_webhook_endpoint,
    replay_webhook_delivery,
    rotate_webhook_secret,
)
from escrow.organizations.models import OrganizationMember
from escrow.organizations.services import MembershipNotFoundError, current_membership_for


def _current_owner(request: HttpRequest) -> OrganizationMember | HttpResponse:
    try:
        membership = current_membership_for(cast(User, request.user))
    except MembershipNotFoundError:
        return error_response("organization_membership_required", 403)
    if membership.role != OrganizationMember.Role.OWNER:
        return error_response("organization_role_forbidden", 403)
    return membership


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _api_key_payload(api_key: ApiKey) -> dict[str, object]:
    return {
        "id": str(api_key.id),
        "name": api_key.name,
        "prefix": api_key.prefix,
        "scopes": api_key.scopes,
        "expires_at": _isoformat(api_key.expires_at),
        "last_used_at": _isoformat(api_key.last_used_at),
        "last_used_ip": api_key.last_used_ip,
        "status": api_key.status,
        "created_at": _isoformat(api_key.created_at),
    }


def _parse_expiry(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timezone.is_naive(parsed):
        raise ValueError
    if parsed <= timezone.now():
        raise ValueError
    return parsed


def _validate_creation(payload: dict[str, object]) -> tuple[str, list[str], datetime | None]:
    name = payload.get("name")
    scopes = payload.get("scopes")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError
    if (
        not isinstance(scopes, list)
        or not scopes
        or not all(isinstance(scope, str) and scope in VALID_API_KEY_SCOPES for scope in scopes)
        or len(set(scopes)) != len(scopes)
    ):
        raise ValueError
    return name.strip(), list(scopes), _parse_expiry(payload.get("expires_at"))


@require_http_methods(["GET", "POST"])
@csrf_protect
@session_required
def api_keys(request: HttpRequest) -> HttpResponse:
    """List or issue credentials exclusively within the session's organization."""
    membership = _current_owner(request)
    if isinstance(membership, HttpResponse):
        return membership
    if request.method == "GET":
        keys = ApiKey.objects.filter(organization=membership.organization)
        return JsonResponse({"api_keys": [_api_key_payload(api_key) for api_key in keys]})
    try:
        payload = parse_json_body(request)
        name, scopes, expires_at = _validate_creation(payload)
    except (InvalidJsonBody, ValueError, TypeError):
        return error_response("validation_error", 400)
    try:
        api_key, raw_secret = create_api_key(
            membership.organization,
            name=name,
            scopes=scopes,
            expires_at=expires_at,
        )
    except ActiveApiKeyLimitError:
        return error_response("active_api_key_limit", 409)
    return JsonResponse({"api_key": _api_key_payload(api_key), "secret": raw_secret}, status=201)


def _api_key_for_owner(key_id: str, membership: OrganizationMember) -> ApiKey | HttpResponse:
    try:
        return ApiKey.objects.get(id=key_id, organization=membership.organization)
    except (ApiKey.DoesNotExist, ValueError):
        return error_response("not_found", 404)


@require_http_methods(["POST"])
@csrf_protect
@session_required
def rotate(request: HttpRequest, key_id: str) -> HttpResponse:
    """Replace a key and bound the old credential's overlap period."""
    membership = _current_owner(request)
    if isinstance(membership, HttpResponse):
        return membership
    api_key = _api_key_for_owner(key_id, membership)
    if isinstance(api_key, HttpResponse):
        return api_key
    try:
        payload = parse_json_body(request) if request.body else {}
        overlap = payload.get("overlap_seconds", settings.API_KEY_ROTATION_OVERLAP_SECONDS)
        if not isinstance(overlap, int) or isinstance(overlap, bool) or not 0 <= overlap <= 86_400:
            raise ValueError
        new_key, raw_secret, previous_key = rotate_api_key(api_key, overlap_seconds=overlap)
    except InvalidJsonBody:
        return error_response("invalid_json", 400)
    except ValueError:
        return error_response("validation_error", 400)
    except ActiveApiKeyLimitError:
        return error_response("active_api_key_limit", 409)
    return JsonResponse(
        {
            "api_key": _api_key_payload(new_key),
            "previous_api_key": _api_key_payload(previous_key),
            "secret": raw_secret,
        },
        status=201,
    )


@require_http_methods(["POST"])
@csrf_protect
@session_required
def revoke(request: HttpRequest, key_id: str) -> HttpResponse:
    """Revoke a credential; retries preserve the same revoked state."""
    membership = _current_owner(request)
    if isinstance(membership, HttpResponse):
        return membership
    api_key = _api_key_for_owner(key_id, membership)
    if isinstance(api_key, HttpResponse):
        return api_key
    return JsonResponse({"api_key": _api_key_payload(revoke_api_key(api_key))})


class OrganizationSummarySerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    name = serializers.CharField()


class IntegrationOrganizationSerializer(serializers.Serializer[Any]):
    organization = OrganizationSummarySerializer()


@extend_schema(
    operation_id="getIntegrationOrganization",
    parameters=[
        OpenApiParameter(
            name="Authorization",
            type=str,
            location=OpenApiParameter.HEADER,
            required=True,
            description="Bearer esk_<prefix>_<secret>",
        )
    ],
    auth=[{"ApiKeyAuth": []}],  # type: ignore[list-item]  # drf-spectacular annotation is narrow.
    responses={200: IntegrationOrganizationSerializer},
)
@api_view(["GET"])
@authentication_classes([])
@permission_classes([])
def integration_organization(request: HttpRequest) -> Response | HttpResponse:
    """Example tenant-scoped API endpoint used to verify bearer-key contracts."""
    authenticated = authenticate_api_key(request, required_scope="agreements:read")
    if isinstance(authenticated, HttpResponse):
        return authenticated
    assert isinstance(authenticated, ApiKeyAuthentication)
    organization = authenticated.api_key.organization
    return Response({"organization": {"id": str(organization.id), "name": organization.name}})


def _webhook_endpoint_payload(endpoint: WebhookEndpoint) -> dict[str, object]:
    return {
        "id": str(endpoint.id),
        "url": endpoint.url,
        "is_active": endpoint.is_active,
        "previous_secret_expires_at": _isoformat(endpoint.previous_secret_expires_at),
        "created_at": _isoformat(endpoint.created_at),
    }


def _webhook_delivery_payload(delivery: WebhookDelivery) -> dict[str, object]:
    return {
        "id": str(delivery.id),
        "endpoint_id": str(delivery.endpoint_id),
        "event_id": str(delivery.event_id),
        "agreement_id": str(delivery.event.agreement_id),
        "event_type": delivery.event.event_type,
        "sequence": delivery.event.sequence,
        "status": delivery.status,
        "attempts": delivery.attempts,
        "next_attempt_at": _isoformat(delivery.next_attempt_at),
        "delivered_at": _isoformat(delivery.delivered_at),
        "last_response_status": delivery.last_response_status,
        "last_error": delivery.last_error,
        "replay_count": delivery.replay_count,
    }


@require_http_methods(["GET", "POST"])
@csrf_protect
@session_required
def webhook_endpoints(request: HttpRequest) -> HttpResponse:
    """Manage owner-only webhook destinations without ever re-displaying a secret."""
    membership = _current_owner(request)
    if isinstance(membership, HttpResponse):
        return membership
    if request.method == "GET":
        endpoints = WebhookEndpoint.objects.filter(organization=membership.organization)
        return JsonResponse(
            {"webhook_endpoints": [_webhook_endpoint_payload(item) for item in endpoints]}
        )
    try:
        payload = parse_json_body(request)
        if set(payload) != {"url"}:
            raise ValueError
        endpoint, raw_secret = create_webhook_endpoint(
            organization=membership.organization,
            url=payload["url"],
        )
    except (InvalidJsonBody, ValueError, WebhookEndpointValidationError):
        return error_response("validation_error", 400)
    except IntegrityError:
        return error_response("webhook_endpoint_exists", 409)
    record_audit_event(
        event_type="webhook_endpoint_created",
        organization=membership.organization,
        actor=cast(User, request.user),
        correlation_id=request.headers.get("X-Correlation-ID", ""),
        payload={"endpoint_id": str(endpoint.id)},
    )
    return JsonResponse(
        {"webhook_endpoint": _webhook_endpoint_payload(endpoint), "secret": raw_secret}, status=201
    )


def _webhook_endpoint_for_owner(
    endpoint_id: str,
    membership: OrganizationMember,
) -> WebhookEndpoint | HttpResponse:
    try:
        return WebhookEndpoint.objects.get(id=endpoint_id, organization=membership.organization)
    except (WebhookEndpoint.DoesNotExist, ValueError):
        return error_response("not_found", 404)


@require_http_methods(["POST"])
@csrf_protect
@session_required
def rotate_webhook(request: HttpRequest, endpoint_id: str) -> HttpResponse:
    """Issue one replacement signing secret with a bounded, auditable overlap."""
    membership = _current_owner(request)
    if isinstance(membership, HttpResponse):
        return membership
    endpoint = _webhook_endpoint_for_owner(endpoint_id, membership)
    if isinstance(endpoint, HttpResponse):
        return endpoint
    try:
        payload = parse_json_body(request) if request.body else {}
        overlap_seconds = payload.get("overlap_seconds", 3600)
        rotated, raw_secret = rotate_webhook_secret(endpoint, overlap_seconds=overlap_seconds)
    except (InvalidJsonBody, WebhookEndpointValidationError, ValueError):
        return error_response("validation_error", 400)
    record_audit_event(
        event_type="webhook_secret_rotated",
        organization=membership.organization,
        actor=cast(User, request.user),
        correlation_id=request.headers.get("X-Correlation-ID", ""),
        payload={"endpoint_id": str(rotated.id)},
    )
    return JsonResponse(
        {"webhook_endpoint": _webhook_endpoint_payload(rotated), "secret": raw_secret}
    )


@require_http_methods(["GET"])
@session_required
def webhook_deliveries(request: HttpRequest) -> HttpResponse:
    """Show tenant-safe delivery status for the organization operations dashboard."""
    try:
        membership = current_membership_for(cast(User, request.user))
    except MembershipNotFoundError:
        return error_response("organization_membership_required", 403)
    deliveries = WebhookDelivery.objects.select_related("event", "endpoint").filter(
        endpoint__organization=membership.organization
    )
    return JsonResponse(
        {"webhook_deliveries": [_webhook_delivery_payload(item) for item in deliveries]}
    )


@require_http_methods(["POST"])
@csrf_protect
@session_required
def replay_webhook(request: HttpRequest, delivery_id: str) -> HttpResponse:
    """Replay only a failed delivery while retaining its immutable event identity."""
    membership = _current_owner(request)
    if isinstance(membership, HttpResponse):
        return membership
    try:
        delivery = WebhookDelivery.objects.select_related("event", "endpoint").get(
            id=delivery_id,
            endpoint__organization=membership.organization,
        )
    except (WebhookDelivery.DoesNotExist, ValueError):
        return error_response("not_found", 404)
    replayed = replay_webhook_delivery(
        delivery,
        correlation_id=request.headers.get("X-Correlation-ID", ""),
    )
    return JsonResponse({"webhook_delivery": _webhook_delivery_payload(replayed)})
