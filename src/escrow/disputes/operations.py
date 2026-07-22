"""Internal session-authenticated transport for private evidence access."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from escrow.agreements.services import AgreementValidationError, validate_idempotency_key
from escrow.correlation import get_correlation_id
from escrow.disputes.services import (
    DisputeAgreementNotFound,
    DisputeRecommendationConflict,
    DisputeRecommendationForbidden,
    DisputeRecommendationValidationError,
    DisputeStateConflict,
    EvidenceAccessExpired,
    EvidenceAccessForbidden,
    EvidenceNotFound,
    download_evidence_with_grant,
    get_dispute_analyst_dashboard,
    issue_evidence_access_grant,
    submit_dispute_recommendation,
)
from escrow.disputes.storage import evidence_s3_client
from escrow.http import InvalidJsonBody, error_response, parse_json_body, session_required
from escrow.identity.models import User



@require_POST
@csrf_protect
@session_required
def issue_evidence_access_grant_view(
    request: HttpRequest,
    dispute_id: UUID,
    evidence_id: UUID,
) -> HttpResponse:
    """Give one authorized staff member a short-lived evidence capability."""
    try:
        grant, access_token = issue_evidence_access_grant(
            dispute_id=dispute_id,
            evidence_id=evidence_id,
            actor=cast(User, request.user),
            correlation_id=get_correlation_id(),
        )
    except EvidenceAccessForbidden:
        return error_response("evidence_access_forbidden", 403)
    except EvidenceNotFound:
        return error_response("not_found", 404)
    response = JsonResponse(
        {"access_token": access_token, "expires_at": grant.expires_at},
        status=201,
    )
    response["Cache-Control"] = "no-store, private"
    response["Referrer-Policy"] = "no-referrer"
    return response


@require_GET
def download_evidence_with_grant_view(request: HttpRequest, access_token: str) -> HttpResponse:
    """Exchange a valid grant for one audited, short-lived pre-signed URL."""
    try:
        download_url, grant = download_evidence_with_grant(
            access_token=access_token,
            s3_client=evidence_s3_client(),
            correlation_id=get_correlation_id(),
        )
    except EvidenceNotFound:
        return error_response("not_found", 404)
    except EvidenceAccessExpired:
        return error_response("evidence_access_expired", 410)
    response = JsonResponse({"download_url": download_url, "expires_at": grant.expires_at})
    response["Cache-Control"] = "no-store, private"
    response["Referrer-Policy"] = "no-referrer"
    return response


@require_GET
@session_required
def dispute_analyst_dashboard_view(request: HttpRequest) -> HttpResponse:
    """Return SLA counts and masked dispute queues for authorized analysts."""
    try:
        dashboard = get_dispute_analyst_dashboard(analyst=cast(User, request.user))
    except (EvidenceAccessForbidden, DisputeRecommendationForbidden):
        return error_response("risk_analyst_required", 403)
    return JsonResponse(dashboard)


@require_POST
@csrf_protect
@session_required
def submit_dispute_recommendation_view(request: HttpRequest, dispute_id: UUID) -> HttpResponse:
    """Submit an analyst recommendation for one dispute."""
    command_header = request.headers.get("Idempotency-Key")
    if command_header is None or not command_header.strip():
        return error_response("idempotency_key_required", 400)
    try:
        payload = parse_json_body(request)
        if set(payload) != {"recommendation", "rationale"}:
            raise InvalidJsonBody
        recommendation = payload["recommendation"]
        rationale = payload["rationale"]
        command_id = validate_idempotency_key(command_header)
        if not isinstance(recommendation, str) or not isinstance(rationale, str):
            raise InvalidJsonBody
    except (AgreementValidationError, InvalidJsonBody):
        return error_response("validation_error", 400)
    try:
        rec, replayed = submit_dispute_recommendation(
            dispute_id=dispute_id,
            analyst=cast(User, request.user),
            recommendation=recommendation,
            command_id=command_id,
            rationale=rationale,
            correlation_id=get_correlation_id(),
        )
    except (EvidenceAccessForbidden, DisputeRecommendationForbidden):
        return error_response("risk_analyst_required", 403)
    except DisputeAgreementNotFound:
        return error_response("not_found", 404)
    except (DisputeRecommendationConflict, DisputeStateConflict):
        return error_response("dispute_recommendation_conflict", 409)
    except DisputeRecommendationValidationError:
        return error_response("validation_error", 400)

    return JsonResponse(
        {
            "recommendation": {
                "id": str(rec.id),
                "dispute_id": str(rec.dispute_id),
                "recommendation": rec.recommendation,
                "replayed": replayed,
            }
        },
        status=202,
    )

