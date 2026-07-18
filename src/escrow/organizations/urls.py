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
        "current/webhooks/",
        integration_views.webhook_endpoints,
        name="organization-webhook-endpoints",
    ),
    path(
        "current/webhooks/<uuid:endpoint_id>/rotate/",
        integration_views.rotate_webhook,
        name="organization-webhook-rotate",
    ),
    path(
        "current/webhook-deliveries/",
        integration_views.webhook_deliveries,
        name="organization-webhook-deliveries",
    ),
    path(
        "current/webhook-deliveries/<uuid:delivery_id>/replay/",
        integration_views.replay_webhook,
        name="organization-webhook-replay",
    ),
    path(
        "current/members/<uuid:member_id>/", views.member_detail, name="organization-member-detail"
    ),
]
