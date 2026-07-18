"""Organization-scoped dashboard and membership management endpoints."""

from __future__ import annotations

import uuid
from typing import Any, cast

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_http_methods

from escrow.http import InvalidJsonBody, error_response, parse_json_body, session_required
from escrow.identity.models import User
from escrow.organizations.models import OrganizationMember
from escrow.organizations.services import (
    MembershipNotFoundError,
    current_membership_for,
    membership_for_current_organization,
)


def _current(request: HttpRequest) -> OrganizationMember | HttpResponse:
    try:
        return current_membership_for(cast(User, request.user))
    except MembershipNotFoundError:
        return error_response("organization_membership_required", 403)


def _owner_or_error(membership: OrganizationMember) -> HttpResponse | None:
    if membership.role != OrganizationMember.Role.OWNER:
        return error_response("organization_role_forbidden", 403)
    return None


def _member_payload(membership: OrganizationMember) -> dict[str, str]:
    return {
        "id": str(membership.id),
        "email": membership.user.email,
        "role": membership.role,
    }


@require_GET
@session_required
def current_organization(request: HttpRequest) -> HttpResponse:
    """Return only the authenticated member's organization dashboard baseline."""
    membership = _current(request)
    if isinstance(membership, HttpResponse):
        return membership
    return JsonResponse(
        {
            "organization": {
                "id": str(membership.organization.id),
                "name": membership.organization.name,
                "document_masked": None,
            },
            "membership": {"id": str(membership.id), "role": membership.role},
            "balances": {"held_brl_minor": 0, "available_brl_minor": 0},
            "upcoming_releases": [],
        }
    )


@require_http_methods(["GET", "POST"])
@csrf_protect
@session_required
def members(request: HttpRequest) -> HttpResponse:
    """List or add a pre-registered member inside the current tenant only."""
    membership = _current(request)
    if isinstance(membership, HttpResponse):
        return membership
    if request.method == "GET":
        members = membership.organization.memberships.select_related("user")
        return JsonResponse({"members": [_member_payload(item) for item in members]})
    forbidden = _owner_or_error(membership)
    if forbidden is not None:
        return forbidden
    try:
        payload = parse_json_body(request)
    except InvalidJsonBody:
        return error_response("invalid_json", 400)
    email = payload.get("email")
    role = payload.get("role")
    if not isinstance(email, str) or not isinstance(role, str):
        return error_response(
            "validation_error",
            400,
            errors={
                "email": ["Informe um membro registrado."],
                "role": ["Informe um papel válido."],
            },
        )
    if role not in OrganizationMember.Role.values:
        return error_response(
            "validation_error", 400, errors={"role": ["Informe um papel válido."]}
        )
    user = get_user_model().objects.filter(email=email.casefold()).first()
    if user is None:
        return error_response("member_not_registered", 404)
    try:
        new_membership = OrganizationMember.objects.create(
            organization=membership.organization,
            user=user,
            role=role,
        )
    except IntegrityError:
        return error_response("member_already_exists", 409)
    return JsonResponse({"member": _member_payload(new_membership)}, status=201)


@require_http_methods(["GET", "PATCH", "DELETE"])
@csrf_protect
@session_required
def member_detail(request: HttpRequest, member_id: uuid.UUID) -> HttpResponse:
    """Read or manage a member without ever traversing tenant boundaries."""
    current = _current(request)
    if isinstance(current, HttpResponse):
        return current
    try:
        selected = membership_for_current_organization(member_id, current)
    except (OrganizationMember.DoesNotExist, ValueError):
        return error_response("not_found", 404)
    if request.method == "GET":
        return JsonResponse({"member": _member_payload(selected)})
    forbidden = _owner_or_error(current)
    if forbidden is not None:
        return forbidden
    if request.method == "DELETE":
        if selected.role == OrganizationMember.Role.OWNER:
            owner_count = current.organization.memberships.filter(
                role=OrganizationMember.Role.OWNER
            ).count()
            if owner_count == 1:
                return error_response("last_owner_required", 409)
        selected.delete()
        return HttpResponse(status=204)
    try:
        payload = parse_json_body(request)
    except InvalidJsonBody:
        return error_response("invalid_json", 400)
    role: Any = payload.get("role")
    if not isinstance(role, str) or role not in OrganizationMember.Role.values:
        return error_response(
            "validation_error", 400, errors={"role": ["Informe um papel válido."]}
        )
    if selected.role == OrganizationMember.Role.OWNER and role != OrganizationMember.Role.OWNER:
        owner_count = current.organization.memberships.filter(
            role=OrganizationMember.Role.OWNER
        ).count()
        if owner_count == 1:
            return error_response("last_owner_required", 409)
    selected.role = role
    selected.save(update_fields=["role", "updated_at"])
    return JsonResponse({"member": _member_payload(selected)})
