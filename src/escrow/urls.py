"""Top-level HTTP routes kept outside financial bounded contexts."""

from __future__ import annotations

from django.urls import path

from escrow.health import liveness, readiness

urlpatterns = [
    path("health/live/", liveness, name="health-live"),
    path("health/ready/", readiness, name="health-ready"),
]
