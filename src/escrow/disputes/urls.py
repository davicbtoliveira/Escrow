"""Internal operations routes for private dispute evidence."""

from __future__ import annotations

from django.urls import path

from escrow.disputes import operations

urlpatterns = [
    path(
        "dashboard/",
        operations.dispute_analyst_dashboard_view,
        name="dispute-analyst-dashboard",
    ),
    path(
        "<uuid:dispute_id>/recommendation/",
        operations.submit_dispute_recommendation_view,
        name="dispute-recommendation-submit",
    ),
    path(
        "<uuid:dispute_id>/evidence/<uuid:evidence_id>/access-grants/",
        operations.issue_evidence_access_grant_view,
        name="evidence-access-grant-issue",
    ),
    path(
        "evidence-access/<str:access_token>/download/",
        operations.download_evidence_with_grant_view,
        name="evidence-access-download",
    ),
]

