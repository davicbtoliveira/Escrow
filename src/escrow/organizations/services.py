"""Tenant-scoped membership selectors and role guards."""

from __future__ import annotations

import uuid

from escrow.identity.models import User
from escrow.organizations.models import OrganizationMember


class MembershipNotFoundError(LookupError):
    """The session user has no active organization workspace."""


def current_membership_for(user: User) -> OrganizationMember:
    """Resolve the first active organization for this MVP's single-workspace session."""
    membership = (
        OrganizationMember.objects.select_related("organization")
        .filter(user=user, organization__is_active=True)
        .order_by("created_at", "id")
        .first()
    )
    if membership is None:
        raise MembershipNotFoundError
    return membership


def membership_for_current_organization(
    member_id: str | uuid.UUID,
    current: OrganizationMember,
) -> OrganizationMember:
    """Keep membership lookups constrained to the session's organization."""
    return OrganizationMember.objects.select_related("user").get(
        id=member_id,
        organization=current.organization,
    )
