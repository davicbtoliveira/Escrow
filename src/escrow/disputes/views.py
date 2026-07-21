"""Customer dispute transport boundary."""

from __future__ import annotations

from uuid import UUID

import redis
from django.http import HttpRequest, HttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from escrow.agreements.pii import PiiEncryptionUnavailable
from escrow.agreements.services import find_checkout_agreement
from escrow.correlation import get_correlation_id
from escrow.delivery.emails import CustomerOtpDeliveryError
from escrow.delivery.models import CustomerOtpChallenge
from escrow.delivery.services import (
    CustomerAcceptanceAuthorizationInvalid,
    CustomerOtpChallengeNotFound,
    CustomerOtpStateConflict,
    CustomerOtpVerificationFailed,
    DeliveryAgreementNotFound,
    request_customer_acceptance_otp,
    verify_customer_acceptance_otp,
)
from escrow.disputes.services import (
    DisputeAlreadyOpen,
    DisputeStateConflict,
    open_customer_dispute,
)
from escrow.http import InvalidJsonBody, error_response, parse_json_body
from escrow.integrations.rate_limit import (
    check_customer_otp_send_rate_limit,
    check_customer_otp_verify_rate_limit,
)


class DisputeOtpRequestResponseSerializer(serializers.Serializer[object]):
    challenge_id = serializers.UUIDField()
    expires_at = serializers.DateTimeField()


class DisputeOtpVerificationSerializer(serializers.Serializer[object]):
    code = serializers.CharField(min_length=6, max_length=6)


class DisputeOtpVerificationResponseSerializer(serializers.Serializer[object]):
    dispute_token = serializers.CharField()


class CustomerDisputeOpenSerializer(serializers.Serializer[object]):
    challenge_id = serializers.UUIDField()
    dispute_token = serializers.CharField()


class CustomerDisputeOpenResponseSerializer(serializers.Serializer[object]):
    dispute_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=["OPEN"])
    opened_at = serializers.DateTimeField()
    sla_due_at = serializers.DateTimeField()


@extend_schema(
    operation_id="requestCustomerDisputeOtp",
    auth=[],
    request=None,
    responses={202: DisputeOtpRequestResponseSerializer},
)
@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def request_customer_dispute_otp(
    request: HttpRequest,
    checkout_token: str,
) -> Response | HttpResponse:
    """Email the customer a proof required before they can open a dispute."""
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
            purpose=CustomerOtpChallenge.Purpose.DISPUTE,
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
    operation_id="verifyCustomerDisputeOtp",
    auth=[],
    request=DisputeOtpVerificationSerializer,
    responses={200: DisputeOtpVerificationResponseSerializer},
)
@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def verify_customer_dispute_otp(
    request: HttpRequest,
    checkout_token: str,
    challenge_id: UUID,
) -> Response | HttpResponse:
    """Turn a valid emailed code into a short-lived one-time dispute proof."""
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
            purpose=CustomerOtpChallenge.Purpose.DISPUTE,
        )
    except (DeliveryAgreementNotFound, CustomerOtpChallengeNotFound):
        return _checkout_headers(error_response("not_found", 404))
    except CustomerOtpVerificationFailed:
        return _checkout_headers(error_response("customer_otp_invalid", 409))
    except CustomerOtpStateConflict:
        return _checkout_headers(error_response("customer_otp_unavailable", 409))
    except PiiEncryptionUnavailable:
        return _checkout_headers(error_response("customer_otp_unavailable", 503))
    return _checkout_headers(Response({"dispute_token": result.acceptance_token}))


@extend_schema(
    operation_id="openCustomerDispute",
    auth=[],
    request=CustomerDisputeOpenSerializer,
    responses={201: CustomerDisputeOpenResponseSerializer},
)
@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def open_customer_dispute_view(
    request: HttpRequest,
    checkout_token: str,
) -> Response | HttpResponse:
    """Freeze a live inspection with one dispute after a fresh email verification."""
    try:
        payload = parse_json_body(request)
        if set(payload) != {"challenge_id", "dispute_token"}:
            raise InvalidJsonBody
        challenge_id = UUID(str(payload["challenge_id"]))
        dispute_token = payload["dispute_token"]
        if not isinstance(dispute_token, str):
            raise InvalidJsonBody
    except (InvalidJsonBody, ValueError):
        return _checkout_headers(error_response("validation_error", 400))
    try:
        result = open_customer_dispute(
            checkout_token=checkout_token,
            challenge_id=challenge_id,
            dispute_token=dispute_token,
            correlation_id=get_correlation_id(),
        )
    except (DeliveryAgreementNotFound, CustomerOtpChallengeNotFound):
        return _checkout_headers(error_response("not_found", 404))
    except CustomerAcceptanceAuthorizationInvalid:
        return _checkout_headers(error_response("customer_dispute_unauthorized", 403))
    except (DisputeAlreadyOpen, DisputeStateConflict):
        return _checkout_headers(error_response("dispute_conflict", 409))
    except PiiEncryptionUnavailable:
        return _checkout_headers(error_response("customer_dispute_unavailable", 503))
    return _checkout_headers(
        Response(
            {
                "dispute_id": str(result.dispute.id),
                "status": result.dispute.status,
                "opened_at": result.dispute.opened_at,
                "sla_due_at": result.dispute.sla_due_at,
            },
            status=201,
        )
    )


def _checkout_headers(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store, private"
    response["Referrer-Policy"] = "no-referrer"
    return response
