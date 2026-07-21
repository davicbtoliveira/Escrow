"""Internal session-authenticated transport for private evidence access."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from escrow.correlation import get_correlation_id
from escrow.disputes.services import (
    EvidenceAccessExpired,
    EvidenceAccessForbidden,
    EvidenceNotFound,
    download_evidence_with_grant,
    issue_evidence_access_grant,
)
from escrow.disputes.storage import evidence_s3_client
from escrow.http import error_response, session_required
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
