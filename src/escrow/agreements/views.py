"""Public HTTP commands for organization agreement creation and customer checkout."""

from __future__ import annotations

from typing import Any

import redis
from django.http import HttpRequest, HttpResponse
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from escrow.agreements.pii import PiiEncryptionUnavailable
from escrow.agreements.services import (
    AgreementValidationError,
    IdempotencyHashUnavailable,
    IdempotencyKeyReusedError,
    InactiveOrganizationError,
    canonical_payload_hash,
    create_agreement,
    find_checkout_agreement,
    parse_agreement_input,
    public_checkout_payload,
    validate_idempotency_key,
)
from escrow.http import InvalidJsonBody, error_response, parse_json_body
from escrow.integrations.authentication import ApiKeyAuthentication, authenticate_api_key
from escrow.integrations.rate_limit import check_public_checkout_rate_limit
from escrow.payments.presentation import public_payment_for_agreement


class CustomerCreateSerializer(serializers.Serializer[Any]):
    name = serializers.CharField()
    email = serializers.EmailField()
    document = serializers.CharField()


class AgreementCreateSerializer(serializers.Serializer[Any]):
    external_customer_id = serializers.CharField()
    customer = CustomerCreateSerializer()
    amount = serializers.CharField()
    currency = serializers.ChoiceField(choices=["BRL", "USD"])
    delivery_window_days = serializers.IntegerField(min_value=1, max_value=90)


class MaskedCustomerSerializer(serializers.Serializer[Any]):
    name = serializers.CharField()
    email_masked = serializers.CharField()
    document_masked = serializers.CharField()


class AgreementTermsSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    status = serializers.CharField()
    customer = MaskedCustomerSerializer()
    amount = serializers.CharField()
    currency = serializers.ChoiceField(choices=["BRL", "USD"])
    fee_bps = serializers.IntegerField()
    delivery_window_days = serializers.IntegerField()
    delivery_due_at = serializers.DateTimeField(allow_null=True)
    inspection_deadline_at = serializers.DateTimeField(allow_null=True)
    refund_reason = serializers.CharField(allow_null=True)
    release_reason = serializers.CharField(allow_null=True)
    realtime_sequence = serializers.IntegerField(min_value=0)


class AgreementSerializer(AgreementTermsSerializer):
    external_customer_id = serializers.CharField()


class PublicAgreementSerializer(AgreementTermsSerializer):
    pass


class PublicPaymentSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=["PENDING", "CONFIRMED", "REJECTED"])
    amount = serializers.CharField()
    currency = serializers.ChoiceField(choices=["BRL", "USD"])
    pix_copy_paste = serializers.CharField()


class AgreementCreationResponseSerializer(serializers.Serializer[Any]):
    agreement = AgreementSerializer()
    checkout_url = serializers.URLField()


class PublicCheckoutResponseSerializer(serializers.Serializer[Any]):
    agreement = PublicAgreementSerializer()
    payment = PublicPaymentSerializer(required=False)


@extend_schema(
    operation_id="createAgreement",
    parameters=[
        OpenApiParameter(
            name="Authorization",
            type=str,
            location=OpenApiParameter.HEADER,
            required=True,
            description="Bearer esk_<prefix>_<secret>",
        ),
        OpenApiParameter(
            name="Idempotency-Key",
            type=str,
            location=OpenApiParameter.HEADER,
            required=True,
            description="Unique key for one organization agreement creation command.",
        ),
    ],
    auth=[{"ApiKeyAuth": []}],  # type: ignore[list-item]  # drf-spectacular annotation is narrow.
    request=AgreementCreateSerializer,
    responses={201: AgreementCreationResponseSerializer},
)
@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def agreement_collection(request: HttpRequest) -> Response | HttpResponse:
    """Create an immutable agreement through a scoped organization API key."""
    authenticated = authenticate_api_key(request, required_scope="agreements:write")
    if isinstance(authenticated, HttpResponse):
        return authenticated
    assert isinstance(authenticated, ApiKeyAuthentication)
    idempotency_header = request.headers.get("Idempotency-Key")
    if idempotency_header is None or not idempotency_header.strip():
        return error_response("idempotency_key_required", 400)
    try:
        payload = parse_json_body(request)
        command = parse_agreement_input(payload)
        idempotency_key = validate_idempotency_key(idempotency_header)
    except (InvalidJsonBody, AgreementValidationError):
        return error_response("validation_error", 400)
    try:
        result = create_agreement(
            organization_id=authenticated.api_key.organization_id,
            command=command,
            payload_hash=canonical_payload_hash(command),
            idempotency_key=idempotency_key,
        )
    except IdempotencyKeyReusedError:
        return error_response("idempotency_key_reused", 409)
    except InactiveOrganizationError:
        return error_response("api_key_invalid", 401)
    except IdempotencyHashUnavailable:
        return error_response("idempotency_unavailable", 503)
    except PiiEncryptionUnavailable:
        return error_response("pii_encryption_unavailable", 503)
    return Response(result.body, status=result.status)


@extend_schema(
    operation_id="getPublicCheckout",
    auth=[],
    responses={200: PublicCheckoutResponseSerializer},
)
@api_view(["GET"])
@authentication_classes([])
@permission_classes([])
def public_checkout(request: HttpRequest, checkout_token: str) -> Response | HttpResponse:
    """Return a bounded public snapshot for one opaque checkout capability."""
    try:
        decision = check_public_checkout_rate_limit(_request_ip(request))
    except redis.RedisError:
        return _checkout_headers(error_response("public_checkout_rate_limit_unavailable", 503))
    if not decision.allowed:
        response = _checkout_headers(error_response("public_checkout_rate_limited", 429))
        response["Retry-After"] = str(decision.retry_after_seconds)
        return response
    try:
        agreement = find_checkout_agreement(checkout_token)
    except PiiEncryptionUnavailable:
        return _checkout_headers(error_response("pii_encryption_unavailable", 503))
    if agreement is None:
        return _checkout_headers(error_response("not_found", 404))
    payload = public_checkout_payload(agreement)
    payment = public_payment_for_agreement(agreement)
    if payment is not None:
        payload["payment"] = payment
    return _checkout_headers(Response(payload))


def _request_ip(request: HttpRequest) -> str:
    remote_address = request.META.get("REMOTE_ADDR")
    return remote_address if isinstance(remote_address, str) else "unknown"


def _checkout_headers(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store, private"
    response["Referrer-Policy"] = "no-referrer"
    return response
