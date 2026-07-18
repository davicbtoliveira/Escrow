"""HTTP boundaries for the explicitly fictional sandbox PIX provider."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import redis
from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from escrow.agreements.pii import PiiEncryptionUnavailable
from escrow.agreements.services import find_checkout_agreement
from escrow.correlation import get_correlation_id
from escrow.http import InvalidJsonBody, error_response, parse_json_body
from escrow.integrations.authentication import ApiKeyAuthentication, authenticate_api_key
from escrow.integrations.rate_limit import (
    check_public_checkout_rate_limit,
    check_webhook_rate_limit,
)
from escrow.notifications.outbox import enqueue_agreement_status_changed
from escrow.payments.callbacks import CallbackSignatureError, sign_sandbox_callback
from escrow.payments.funding import process_sandbox_pix_callback
from escrow.payments.models import ProviderCallbackReceipt, SandboxPixCharge
from escrow.payments.presentation import public_payment_payload
from escrow.payments.services import (
    ChargeIdempotencyConflict,
    InvalidCallbackTransition,
    InvalidChargeState,
    PaymentValidationError,
    UnknownProviderReference,
    create_sandbox_pix_charge,
)


class PublicSandboxPixPaymentSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=SandboxPixCharge.Status.choices)
    amount = serializers.CharField()
    currency = serializers.ChoiceField(choices=["BRL", "USD"])
    pix_copy_paste = serializers.CharField()


class PublicSandboxPixChargeResponseSerializer(serializers.Serializer[Any]):
    payment = PublicSandboxPixPaymentSerializer()


class SandboxCallbackResponseSerializer(serializers.Serializer[Any]):
    status = serializers.CharField()
    duplicate = serializers.BooleanField()


class SandboxControlSerializer(serializers.Serializer[Any]):
    action = serializers.ChoiceField(choices=["confirm", "delay", "duplicate"])


@extend_schema(
    operation_id="createSandboxPixCharge",
    parameters=[
        OpenApiParameter(
            name="Idempotency-Key",
            type=str,
            location=OpenApiParameter.HEADER,
            required=True,
            description="Replay key for this public checkout PIX instruction.",
        )
    ],
    auth=[],
    request=None,
    responses={202: PublicSandboxPixChargeResponseSerializer},
)
@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def public_checkout_pix_charge(
    request: HttpRequest,
    checkout_token: str,
) -> Response | HttpResponse:
    """Create/replay the only simulated PIX charge behind a checkout capability."""
    rate_limited = _public_checkout_rate_limit(request)
    if rate_limited is not None:
        return rate_limited
    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key is None or not idempotency_key.strip():
        return _checkout_headers(error_response("idempotency_key_required", 400))
    try:
        agreement = find_checkout_agreement(checkout_token)
    except PiiEncryptionUnavailable:
        return _checkout_headers(error_response("pii_encryption_unavailable", 503))
    if agreement is None:
        return _checkout_headers(error_response("not_found", 404))
    try:
        with transaction.atomic():
            result = create_sandbox_pix_charge(
                agreement_id=agreement.id,
                idempotency_key=idempotency_key,
            )
            if not result.replayed:
                agreement.refresh_from_db()
                enqueue_agreement_status_changed(
                    agreement,
                    correlation_id=get_correlation_id(),
                    causation_id=str(result.charge.id),
                )
    except (ChargeIdempotencyConflict, InvalidChargeState, PaymentValidationError):
        return _checkout_headers(error_response("sandbox_charge_conflict", 409))
    return _checkout_headers(
        Response({"payment": public_payment_payload(result.charge)}, status=202)
    )


@extend_schema(
    operation_id="receiveSandboxPixCallback",
    auth=[],
    request=None,
    responses={202: SandboxCallbackResponseSerializer},
)
@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def sandbox_pix_callback(request: HttpRequest) -> Response | HttpResponse:
    """Verify a provider-style callback and atomically enqueue risk processing."""
    if not settings.SANDBOX_PIX_ENABLED or not settings.SANDBOX_PIX_CALLBACK_SIGNING_SECRET:
        return error_response("sandbox_callback_unavailable", 503)
    try:
        decision = check_webhook_rate_limit(_request_ip(request))
    except redis.RedisError:
        return error_response("webhook_rate_limit_unavailable", 503)
    if not decision.allowed:
        response = error_response("webhook_rate_limited", 429)
        response["Retry-After"] = str(decision.retry_after_seconds)
        return response
    timestamp = request.headers.get("X-Sandbox-PIX-Timestamp")
    signature = request.headers.get("X-Sandbox-PIX-Signature")
    if timestamp is None or signature is None:
        return error_response("sandbox_callback_invalid", 400)
    try:
        result = process_sandbox_pix_callback(
            raw_body=bytes(request.body),
            timestamp=timestamp,
            signature=signature,
            signing_secret=settings.SANDBOX_PIX_CALLBACK_SIGNING_SECRET,
            correlation_id=get_correlation_id(),
        )
    except (
        CallbackSignatureError,
        InvalidCallbackTransition,
        PaymentValidationError,
        UnknownProviderReference,
    ):
        return error_response("sandbox_callback_invalid", 400)
    return Response({"status": "accepted", "duplicate": result.callback.duplicate}, status=202)


@extend_schema(
    operation_id="controlSandboxPixCharge",
    parameters=[
        OpenApiParameter(
            name="Authorization",
            type=str,
            location=OpenApiParameter.HEADER,
            required=True,
            description="Bearer organization API key with payments:write.",
        )
    ],
    auth=[{"ApiKeyAuth": []}],  # type: ignore[list-item]
    request=SandboxControlSerializer,
    responses={202: SandboxCallbackResponseSerializer},
)
@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def sandbox_pix_control(
    request: HttpRequest,
    charge_id: UUID,
) -> Response | HttpResponse:
    """Let an authorized tenant exercise deterministic local provider scenarios."""
    if not settings.SANDBOX_PIX_ENABLED or not settings.SANDBOX_PIX_CALLBACK_SIGNING_SECRET:
        return error_response("sandbox_control_unavailable", 503)
    authenticated = authenticate_api_key(request, required_scope="payments:write")
    if isinstance(authenticated, HttpResponse):
        return authenticated
    assert isinstance(authenticated, ApiKeyAuthentication)
    try:
        payload = parse_json_body(request)
    except InvalidJsonBody:
        return error_response("validation_error", 400)
    if set(payload) != {"action"} or payload["action"] not in {"confirm", "delay", "duplicate"}:
        return error_response("validation_error", 400)
    action = payload["action"]
    assert isinstance(action, str)
    try:
        charge = SandboxPixCharge.objects.select_related("agreement").get(id=charge_id)
    except SandboxPixCharge.DoesNotExist:
        return error_response("not_found", 404)
    if charge.agreement.organization_id != authenticated.api_key.organization_id:
        return error_response("not_found", 404)
    if action == "delay":
        return Response({"delivery": "DELAYED", "status": charge.status}, status=202)

    receipt = _receipt_for_sandbox_action(charge, action)
    if receipt is None:
        return error_response("sandbox_duplicate_unavailable", 409)
    raw_body = _sandbox_callback_body(
        event_id=receipt["event_id"],
        provider_reference=charge.provider_reference,
        outcome=receipt["outcome"],
    )
    timestamp = str(int(timezone.now().timestamp()))
    try:
        result = process_sandbox_pix_callback(
            raw_body=raw_body,
            timestamp=timestamp,
            signature=sign_sandbox_callback(
                settings.SANDBOX_PIX_CALLBACK_SIGNING_SECRET,
                timestamp,
                raw_body,
            ),
            signing_secret=settings.SANDBOX_PIX_CALLBACK_SIGNING_SECRET,
            correlation_id=get_correlation_id(),
        )
    except (InvalidCallbackTransition, PaymentValidationError):
        return error_response("sandbox_charge_conflict", 409)
    return Response({"status": "accepted", "duplicate": result.callback.duplicate}, status=202)


def _public_checkout_rate_limit(request: HttpRequest) -> HttpResponse | None:
    try:
        decision = check_public_checkout_rate_limit(_request_ip(request))
    except redis.RedisError:
        return _checkout_headers(error_response("public_checkout_rate_limit_unavailable", 503))
    if decision.allowed:
        return None
    response = _checkout_headers(error_response("public_checkout_rate_limited", 429))
    response["Retry-After"] = str(decision.retry_after_seconds)
    return response


def _receipt_for_sandbox_action(
    charge: SandboxPixCharge,
    action: str,
) -> dict[str, str] | None:
    existing = charge.callback_receipts.order_by("received_at", "id").first()
    if action == "confirm":
        if existing is not None:
            return {"event_id": existing.provider_event_id, "outcome": existing.outcome}
        return {
            "event_id": f"sandbox-confirm-{charge.id.hex}",
            "outcome": ProviderCallbackReceipt.Outcome.CONFIRMED,
        }
    if existing is None:
        return None
    return {"event_id": existing.provider_event_id, "outcome": existing.outcome}


def _sandbox_callback_body(
    *,
    event_id: str,
    provider_reference: str,
    outcome: str,
) -> bytes:
    return json.dumps(
        {
            "event_id": event_id,
            "provider_reference": provider_reference,
            "outcome": outcome,
        },
        separators=(",", ":"),
    ).encode()


def _request_ip(request: HttpRequest) -> str:
    remote_address = request.META.get("REMOTE_ADDR")
    return remote_address if isinstance(remote_address, str) else "unknown"


def _checkout_headers(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store, private"
    response["Referrer-Policy"] = "no-referrer"
    return response
