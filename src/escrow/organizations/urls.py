from __future__ import annotations

from django.urls import path

from escrow.organizations import views

urlpatterns = [
    path("current/", views.current_organization, name="organization-current"),
    path("current/members/", views.members, name="organization-members"),
    path(
        "current/members/<uuid:member_id>/", views.member_detail, name="organization-member-detail"
    ),
]
