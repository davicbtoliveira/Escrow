"""Organization-scoped dashboard and membership management endpoints."""

from __future__ import annotations

import uuid
from typing import Any, cast

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.db.models import Sum
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_http_methods

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.money import calculate_release_fee_minor
from escrow.http import InvalidJsonBody, error_response, parse_json_body, session_required
from escrow.identity.models import User
from escrow.ledger.models import LedgerEntry
from escrow.organizations.models import OrganizationMember
from escrow.organizations.services import (
    MembershipNotFoundError,
    current_membership_for,
    latest_simulated_rate,
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
    balances, upcoming_releases = _financial_overview(membership)
    return JsonResponse(
        {
            "organization": {
                "id": str(membership.organization.id),
                "name": membership.organization.name,
                "document_masked": None,
            },
            "membership": {"id": str(membership.id), "role": membership.role},
            "balances": balances,
            "upcoming_releases": upcoming_releases,
            "exchange_rates": _display_exchange_rates(),
        }
    )


def _display_exchange_rates() -> list[dict[str, object]]:
    """Serve only simulated display rates; authoritative balances stay untouched."""
    rates: list[dict[str, object]] = []
    for base_currency, quote_currency in (("BRL", "USD"), ("USD", "BRL")):
        rate = latest_simulated_rate(base_currency, quote_currency)
        if rate is None:
            continue
        rates.append(
            {
                "base_currency": rate.base_currency,
                "quote_currency": rate.quote_currency,
                "rate_micros": rate.rate_micros,
                "recorded_at": rate.recorded_at.isoformat().replace("+00:00", "Z"),
                "is_simulated": rate.is_simulated,
            }
        )
    return rates


def _financial_overview(
    membership: OrganizationMember,
) -> tuple[dict[str, int], list[dict[str, object]]]:
    organization = membership.organization
    balances: dict[str, int] = {}
    for currency in ("BRL", "USD"):
        lowered = currency.lower()
        balances[f"held_{lowered}_minor"] = _account_balance_minor(
            organization.id, "ESCROW_LIABILITY", currency
        )
        balances[f"available_{lowered}_minor"] = _account_balance_minor(
            organization.id, "ORGANIZATION_PAYABLE", currency
        )
        balances[f"fee_{lowered}_minor"] = _account_balance_minor(
            organization.id, "PLATFORM_FEE_REVENUE", currency
        )
    upcoming_releases: list[dict[str, object]] = []
    agreements = (
        EscrowAgreement.objects.filter(
            organization=organization,
            status=EscrowAgreement.Status.INSPECTION,
            inspection_deadline_at__isnull=False,
        )
        .order_by("inspection_deadline_at", "id")
        .only("id", "amount_minor", "currency", "fee_bps", "inspection_deadline_at")
    )
    for agreement in agreements:
        fee_minor = calculate_release_fee_minor(agreement.amount_minor, agreement.fee_bps)
        deadline = agreement.inspection_deadline_at
        if deadline is None:
            continue
        upcoming_releases.append(
            {
                "id": str(agreement.id),
                "currency": agreement.currency,
                "gross_minor": agreement.amount_minor,
                "fee_minor": fee_minor,
                "net_minor": agreement.amount_minor - fee_minor,
                "release_at": deadline.isoformat().replace("+00:00", "Z"),
            }
        )
    return balances, upcoming_releases


def _account_balance_minor(organization_id: uuid.UUID, account_code: str, currency: str) -> int:
    entries = LedgerEntry.objects.filter(
        ledger_transaction__transfer__agreement__organization_id=organization_id,
        account__code=account_code,
        currency=currency,
    ).aggregate(credits=Sum("credit_minor"), debits=Sum("debit_minor"))
    return int(entries["credits"] or 0) - int(entries["debits"] or 0)


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
