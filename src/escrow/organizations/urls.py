from __future__ import annotations

from django.urls import path

from escrow.integrations import views as integration_views
from escrow.organizations import views

urlpatterns = [
    path("current/", views.current_organization, name="organization-current"),
    path("current/members/", views.members, name="organization-members"),
    path(
        "current/api-keys/",
        integration_views.api_keys,
        name="organization-api-keys",
    ),
    path(
        "current/api-keys/<uuid:key_id>/rotate/",
        integration_views.rotate,
        name="organization-api-key-rotate",
    ),
    path(
        "current/api-keys/<uuid:key_id>/revoke/",
        integration_views.revoke,
        name="organization-api-key-revoke",
    ),
    path(
        "current/members/<uuid:member_id>/", views.member_detail, name="organization-member-detail"
    ),
]
