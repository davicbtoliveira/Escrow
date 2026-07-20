from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from escrow.organizations.models import Organization, OrganizationMember


class OrganizationMembershipApiTests(TestCase):
    def setUp(self) -> None:
        self.owner = get_user_model().objects.create_user(
            email="owner@alpha.example", password="Uma senha forte e exclusiva 2026!"
        )
        self.finance_user = get_user_model().objects.create_user(
            email="finance@alpha.example", password="Uma senha forte e exclusiva 2026!"
        )
        self.foreign_owner = get_user_model().objects.create_user(
            email="owner@beta.example", password="Uma senha forte e exclusiva 2026!"
        )
        self.alpha = Organization.objects.create(name="Alpha Comércio")
        self.beta = Organization.objects.create(name="Beta Comércio")
        self.owner_membership = OrganizationMember.objects.create(
            organization=self.alpha,
            user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        OrganizationMember.objects.create(
            organization=self.alpha,
            user=self.finance_user,
            role=OrganizationMember.Role.FINANCE,
        )
        self.foreign_membership = OrganizationMember.objects.create(
            organization=self.beta,
            user=self.foreign_owner,
            role=OrganizationMember.Role.OWNER,
        )

    def post_json(self, path: str, payload: dict[str, str]):
        return self.client.post(path, data=json.dumps(payload), content_type="application/json")

    def test_current_dashboard_is_scoped_to_the_authenticated_organization(self) -> None:
        self.client.force_login(self.owner)

        response = self.client.get("/api/v1/organizations/current/")

        assert response.status_code == 200
        assert response.json()["organization"]["name"] == "Alpha Comércio"
        assert response.json()["membership"]["role"] == "OWNER"
        assert response.json()["balances"] == {
            "held_brl_minor": 0,
            "held_usd_minor": 0,
            "available_brl_minor": 0,
            "available_usd_minor": 0,
            "fee_brl_minor": 0,
            "fee_usd_minor": 0,
        }

    def test_non_owner_cannot_manage_members(self) -> None:
        self.client.force_login(self.finance_user)

        response = self.post_json(
            "/api/v1/organizations/current/members/",
            {"email": "owner@beta.example", "role": "VIEWER"},
        )

        assert response.status_code == 403
        assert response.json()["code"] == "organization_role_forbidden"
        assert (
            OrganizationMember.objects.filter(
                organization=self.alpha, user=self.foreign_owner
            ).count()
            == 0
        )

    def test_owner_can_add_and_change_a_registered_member_role(self) -> None:
        self.client.force_login(self.owner)

        add_response = self.post_json(
            "/api/v1/organizations/current/members/",
            {"email": "owner@beta.example", "role": "VIEWER"},
        )
        member_id = add_response.json()["member"]["id"]
        change_response = self.client.patch(
            f"/api/v1/organizations/current/members/{member_id}/",
            data=json.dumps({"role": "SUPPORT"}),
            content_type="application/json",
        )

        assert add_response.status_code == 201
        assert change_response.status_code == 200
        assert change_response.json()["member"]["role"] == "SUPPORT"
        assert OrganizationMember.objects.get(id=member_id).organization == self.alpha

    def test_owner_cannot_read_or_mutate_a_member_from_another_organization(self) -> None:
        self.client.force_login(self.owner)

        response = self.client.get(
            f"/api/v1/organizations/current/members/{self.foreign_membership.id}/"
        )

        assert response.status_code == 404
        assert response.json()["code"] == "not_found"
