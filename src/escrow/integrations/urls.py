from __future__ import annotations

from django.urls import path

from escrow.integrations import views

urlpatterns = [
    path("organization/", views.integration_organization, name="integration-organization"),
]
