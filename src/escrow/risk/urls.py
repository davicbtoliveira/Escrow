"""Internal operations routes for risk review."""

from __future__ import annotations

from django.urls import path

from escrow.risk import views

urlpatterns = [
    path("funding-reviews/", views.funding_review_queue, name="risk-funding-review-queue"),
    path(
        "funding-reviews/<uuid:decision_id>/resolve/",
        views.resolve_funding_review,
        name="risk-funding-review-resolve",
    ),
]
