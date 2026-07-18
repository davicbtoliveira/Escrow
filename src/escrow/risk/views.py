"""Internal session-authenticated transport for the funding-risk review queue."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from escrow.agreements.services import AgreementValidationError, validate_idempotency_key
from escrow.correlation import get_correlation_id
from escrow.http import InvalidJsonBody, error_response, parse_json_body, session_required
from escrow.identity.models import User
from escrow.risk.services import (
    FundingReviewAlreadyResolved,
    FundingReviewAuthorizationError,
    FundingReviewIdempotencyConflict,
    FundingReviewNotPending,
    FundingReviewQueueItem,
    FundingReviewValidationError,
    list_manual_funding_review_queue,
    resolve_funding_review_and_enqueue,
)


@require_GET
@session_required
def funding_review_queue(request: HttpRequest) -> HttpResponse:
    """Return a masked, globally authorized queue for risk-dispute analysts only."""
    try:
        items = list_manual_funding_review_queue(analyst=cast(User, request.user))
    except FundingReviewAuthorizationError:
        return error_response("risk_analyst_required", 403)
    return JsonResponse({"reviews": [_queue_item_payload(item) for item in items]})


@require_POST
@csrf_protect
@session_required
def resolve_funding_review(request: HttpRequest, decision_id: UUID) -> HttpResponse:
    """Commit a single analyst conclusion and enqueue its next financial command."""
    command_header = request.headers.get("Idempotency-Key")
    if command_header is None or not command_header.strip():
        return error_response("idempotency_key_required", 400)
    try:
        payload = parse_json_body(request)
        if set(payload) != {"outcome", "rationale"}:
            raise InvalidJsonBody
        outcome = payload["outcome"]
        rationale = payload["rationale"]
        command_id = validate_idempotency_key(command_header)
        if not isinstance(outcome, str) or not isinstance(rationale, str):
            raise InvalidJsonBody
    except (AgreementValidationError, InvalidJsonBody):
        return error_response("validation_error", 400)
    try:
        result = resolve_funding_review_and_enqueue(
            decision_id=decision_id,
            analyst=cast(User, request.user),
            outcome=outcome,
            command_id=command_id,
            rationale=rationale,
            correlation_id=get_correlation_id(),
        )
    except FundingReviewAuthorizationError:
        return error_response("risk_analyst_required", 403)
    except (FundingReviewAlreadyResolved, FundingReviewNotPending):
        return error_response("funding_review_conflict", 409)
    except (FundingReviewIdempotencyConflict, FundingReviewValidationError):
        return error_response("validation_error", 400)
    return JsonResponse(
        {
            "review": {
                "id": str(result.review.id),
                "decision_id": str(result.decision.id),
                "outcome": result.review.outcome,
                "replayed": result.replayed,
            }
        },
        status=202,
    )


def _queue_item_payload(item: FundingReviewQueueItem) -> dict[str, object]:
    return {
        "decision_id": str(item.decision_id),
        "transfer_id": str(item.transfer_id),
        "agreement_id": str(item.agreement_id),
        "organization": {
            "id": str(item.organization_id),
            "name_masked": item.organization_name_masked,
        },
        "customer": {
            "name": item.customer_name_masked,
            "email_masked": item.customer_email_masked,
            "document_masked": item.customer_document_masked,
        },
        "amount_minor": item.amount_minor,
        "currency": item.currency,
        "policy_version": item.policy_version,
        "policy_configuration": item.policy_configuration,
        "inputs": item.inputs,
        "score": item.score,
        "reasons": list(item.reasons),
        "evaluated_at": item.evaluated_at.isoformat().replace("+00:00", "Z"),
    }
