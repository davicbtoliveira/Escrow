"""Organization delivery-report transport boundary."""

from __future__ import annotations

from uuid import UUID

import redis
from django.http import HttpRequest, HttpResponse
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from escrow.agreements.pii import PiiEncryptionUnavailable
from escrow.agreements.services import (
    AgreementValidationError,
    find_checkout_agreement,
    validate_idempotency_key,
)
from escrow.correlation import get_correlation_id
from escrow.delivery.emails import CustomerOtpDeliveryError
from escrow.delivery.services import (
    CustomerAcceptanceAuthorizationInvalid,
    CustomerOtpChallengeNotFound,
    CustomerOtpStateConflict,
    CustomerOtpVerificationFailed,
    DeliveryAgreementNotFound,
    DeliveryIdempotencyConflict,
    DeliveryStateConflict,
    accept_customer_delivery,
    report_delivery,
    request_customer_acceptance_otp,
    verify_customer_acceptance_otp,
)
from escrow.http import InvalidJsonBody, error_response, parse_json_body
from escrow.integrations.authentication import ApiKeyAuthentication, authenticate_api_key
from escrow.integrations.rate_limit import (
    check_customer_otp_send_rate_limit,
    check_customer_otp_verify_rate_limit,
)


class DeliveryReportResponseSerializer(serializers.Serializer[object]):
    agreement_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=["INSPECTION"])
    inspection_deadline_at = serializers.DateTimeField()


class CustomerOtpRequestResponseSerializer(serializers.Serializer[object]):
    challenge_id = serializers.UUIDField()
    expires_at = serializers.DateTimeField()


class CustomerOtpVerificationSerializer(serializers.Serializer[object]):
    code = serializers.CharField(min_length=6, max_length=6)


class CustomerOtpVerificationResponseSerializer(serializers.Serializer[object]):
    acceptance_token = serializers.CharField()


class CustomerDeliveryAcceptanceSerializer(serializers.Serializer[object]):
    challenge_id = serializers.UUIDField()
    acceptance_token = serializers.CharField()


class CustomerDeliveryAcceptanceResponseSerializer(serializers.Serializer[object]):
    status = serializers.CharField()
    transfer_id = serializers.UUIDField()


@extend_schema(
    operation_id="reportAgreementDelivery",
    parameters=[
        OpenApiParameter(
            name="Authorization",
            type=str,
            location=OpenApiParameter.HEADER,
            required=True,
            description="Bearer organization API key with agreements:write.",
        ),
        OpenApiParameter(
            name="Idempotency-Key",
            type=str,
            location=OpenApiParameter.HEADER,
            required=True,
            description="Unique key for one delivery declaration.",
        ),
    ],
    auth=[{"ApiKeyAuth": []}],  # type: ignore[list-item]
    request=None,
    responses={202: DeliveryReportResponseSerializer},
)
@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def report_agreement_delivery(
    request: HttpRequest,
    agreement_id: UUID,
) -> Response | HttpResponse:
    """Let the owning organization begin its customer's inspection window."""
    authenticated = authenticate_api_key(request, required_scope="agreements:write")
    if isinstance(authenticated, HttpResponse):
        return authenticated
    assert isinstance(authenticated, ApiKeyAuthentication)
    idempotency_header = request.headers.get("Idempotency-Key")
    if idempotency_header is None:
        return error_response("idempotency_key_required", 400)
    try:
        payload = parse_json_body(request)
        if payload:
            raise InvalidJsonBody
        idempotency_key = validate_idempotency_key(idempotency_header)
    except (AgreementValidationError, InvalidJsonBody):
        return error_response("validation_error", 400)
    try:
        result = report_delivery(
            organization_id=authenticated.api_key.organization_id,
            agreement_id=agreement_id,
            idempotency_key=idempotency_key,
            correlation_id=get_correlation_id(),
        )
    except (DeliveryAgreementNotFound, ValueError):
        return error_response("not_found", 404)
    except (DeliveryIdempotencyConflict, DeliveryStateConflict):
        return error_response("delivery_report_conflict", 409)
    return Response(
        {
            "agreement_id": str(result.agreement.id),
            "status": result.agreement.status,
            "inspection_deadline_at": result.report.inspection_deadline_at,
        },
        status=202,
    )


@extend_schema(
    operation_id="requestCustomerDeliveryAcceptanceOtp",
    auth=[],
    request=None,
    responses={202: CustomerOtpRequestResponseSerializer},
)
@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def request_customer_delivery_acceptance_otp(
    request: HttpRequest,
    checkout_token: str,
) -> Response | HttpResponse:
    """Email the customer a proof required before they can release held funds."""
    try:
        payload = parse_json_body(request)
        if payload:
            raise InvalidJsonBody
        agreement = find_checkout_agreement(checkout_token)
    except (InvalidJsonBody, PiiEncryptionUnavailable):
        return _checkout_headers(error_response("validation_error", 400))
    if agreement is None:
        return _checkout_headers(error_response("not_found", 404))
    try:
        decision = check_customer_otp_send_rate_limit(str(agreement.id))
    except redis.RedisError:
        return _checkout_headers(error_response("customer_otp_rate_limit_unavailable", 503))
    if not decision.allowed:
        response = _checkout_headers(error_response("customer_otp_rate_limited", 429))
        response["Retry-After"] = str(decision.retry_after_seconds)
        return response
    try:
        result = request_customer_acceptance_otp(
            checkout_token=checkout_token,
            correlation_id=get_correlation_id(),
        )
    except DeliveryAgreementNotFound:
        return _checkout_headers(error_response("not_found", 404))
    except CustomerOtpStateConflict:
        return _checkout_headers(error_response("customer_otp_unavailable", 409))
    except (CustomerOtpDeliveryError, PiiEncryptionUnavailable):
        return _checkout_headers(error_response("customer_otp_delivery_unavailable", 503))
    return _checkout_headers(
        Response(
            {
                "challenge_id": str(result.challenge.id),
                "expires_at": result.challenge.expires_at,
            },
            status=202,
        )
    )


@extend_schema(
    operation_id="verifyCustomerDeliveryAcceptanceOtp",
    auth=[],
    request=CustomerOtpVerificationSerializer,
    responses={200: CustomerOtpVerificationResponseSerializer},
)
@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def verify_customer_delivery_acceptance_otp(
    request: HttpRequest,
    checkout_token: str,
    challenge_id: UUID,
) -> Response | HttpResponse:
    """Turn a valid emailed code into a short-lived one-time acceptance proof."""
    try:
        payload = parse_json_body(request)
        if set(payload) != {"code"} or not isinstance(payload["code"], str):
            raise InvalidJsonBody
    except InvalidJsonBody:
        return _checkout_headers(error_response("validation_error", 400))
    try:
        decision = check_customer_otp_verify_rate_limit(str(challenge_id))
    except redis.RedisError:
        return _checkout_headers(error_response("customer_otp_rate_limit_unavailable", 503))
    if not decision.allowed:
        response = _checkout_headers(error_response("customer_otp_rate_limited", 429))
        response["Retry-After"] = str(decision.retry_after_seconds)
        return response
    try:
        result = verify_customer_acceptance_otp(
            checkout_token=checkout_token,
            challenge_id=challenge_id,
            code=payload["code"],
        )
    except (DeliveryAgreementNotFound, CustomerOtpChallengeNotFound):
        return _checkout_headers(error_response("not_found", 404))
    except CustomerOtpVerificationFailed:
        return _checkout_headers(error_response("customer_otp_invalid", 409))
    except CustomerOtpStateConflict:
        return _checkout_headers(error_response("customer_otp_unavailable", 409))
    except PiiEncryptionUnavailable:
        return _checkout_headers(error_response("customer_otp_unavailable", 503))
    return _checkout_headers(Response({"acceptance_token": result.acceptance_token}))


@extend_schema(
    operation_id="acceptCustomerDelivery",
    auth=[],
    request=CustomerDeliveryAcceptanceSerializer,
    responses={202: CustomerDeliveryAcceptanceResponseSerializer},
)
@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def accept_customer_reported_delivery(
    request: HttpRequest,
    checkout_token: str,
) -> Response | HttpResponse:
    """Asynchronously request release after a fresh customer email verification."""
    try:
        payload = parse_json_body(request)
        if set(payload) != {"challenge_id", "acceptance_token"}:
            raise InvalidJsonBody
        challenge_id = UUID(str(payload["challenge_id"]))
        acceptance_token = payload["acceptance_token"]
        if not isinstance(acceptance_token, str):
            raise InvalidJsonBody
    except (InvalidJsonBody, ValueError):
        return _checkout_headers(error_response("validation_error", 400))
    try:
        result = accept_customer_delivery(
            checkout_token=checkout_token,
            challenge_id=challenge_id,
            acceptance_token=acceptance_token,
            correlation_id=get_correlation_id(),
        )
    except (DeliveryAgreementNotFound, CustomerOtpChallengeNotFound):
        return _checkout_headers(error_response("not_found", 404))
    except CustomerAcceptanceAuthorizationInvalid:
        return _checkout_headers(error_response("customer_acceptance_unauthorized", 403))
    except CustomerOtpStateConflict:
        return _checkout_headers(error_response("customer_acceptance_unavailable", 409))
    except PiiEncryptionUnavailable:
        return _checkout_headers(error_response("customer_acceptance_unavailable", 503))
    return _checkout_headers(
        Response(
            {"status": "PROCESSING", "transfer_id": str(result.transfer.id)},
            status=202,
        )
    )


def _checkout_headers(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store, private"
    response["Referrer-Policy"] = "no-referrer"
    return response
