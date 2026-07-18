"""Top-level HTTP routes kept outside financial bounded contexts."""

from __future__ import annotations

from django.urls import include, path

from escrow.health import liveness, readiness

urlpatterns = [
    path("health/live/", liveness, name="health-live"),
    path("health/ready/", readiness, name="health-ready"),
    path("api/v1/auth/", include("escrow.identity.urls")),
    path("api/v1/organizations/", include("escrow.organizations.urls")),
]
