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
        "admin-dashboard/",
        operations.dispute_admin_dashboard_view,
        name="dispute-admin-dashboard",
    ),
    path(
        "<uuid:dispute_id>/recommendation/",
        operations.submit_dispute_recommendation_view,
        name="dispute-recommendation-submit",
    ),
    path(
        "<uuid:dispute_id>/decrypt-pii/",
        operations.decrypt_dispute_customer_pii_view,
        name="dispute-decrypt-pii",
    ),
    path(
        "<uuid:dispute_id>/resolve/",
        operations.resolve_dispute_admin_decision_view,
        name="dispute-admin-resolve",
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


