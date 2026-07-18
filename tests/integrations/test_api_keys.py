from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from escrow.integrations.models import ApiKey
from escrow.integrations.rate_limit import RateLimitDecision
from escrow.organizations.models import Organization, OrganizationMember


class ApiKeyApiTests(TestCase):
    def setUp(self) -> None:
        self.owner = get_user_model().objects.create_user(
            email="owner@alpha.example",
            password="Uma senha forte e exclusiva 2026!",
        )
        self.viewer = get_user_model().objects.create_user(
            email="viewer@alpha.example",
            password="Uma senha forte e exclusiva 2026!",
        )
        self.organization = Organization.objects.create(name="Alpha Comércio")
        OrganizationMember.objects.create(
            organization=self.organization,
            user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        OrganizationMember.objects.create(
            organization=self.organization,
            user=self.viewer,
            role=OrganizationMember.Role.VIEWER,
        )
        self.collection_url = "/api/v1/organizations/current/api-keys/"

    def post_json(self, path: str, payload: dict[str, object]):
        return self.client.post(path, data=json.dumps(payload), content_type="application/json")

    def create_key(self, **overrides: object):
        payload: dict[str, object] = {
            "name": "Integração marketplace",
            "scopes": ["agreements:read"],
        }
        payload.update(overrides)
        return self.post_json(self.collection_url, payload)

    def test_owner_sees_the_secret_once_but_only_its_hash_is_persisted(self) -> None:
        self.client.force_login(self.owner)

        create_response = self.create_key(expires_at="2030-01-01T00:00:00Z")
        secret = create_response.json()["secret"]
        list_response = self.client.get(self.collection_url)
        stored_key = ApiKey.objects.get()

        assert create_response.status_code == 201
        assert secret.startswith(f"esk_{stored_key.prefix}_")
        assert stored_key.secret_hash != secret
        assert stored_key.scopes == ["agreements:read"]
        assert list_response.status_code == 200
        assert "secret" not in list_response.json()["api_keys"][0]
        assert list_response.json()["api_keys"][0]["expires_at"] == "2030-01-01T00:00:00Z"

    def test_owner_cannot_create_a_third_active_key_but_can_revoke_one(self) -> None:
        self.client.force_login(self.owner)
        first_response = self.create_key(name="Primeira")
        self.create_key(name="Segunda")

        rejected_response = self.create_key(name="Terceira")
        revoke_response = self.post_json(
            f"{self.collection_url}{first_response.json()['api_key']['id']}/revoke/",
            {},
        )
        accepted_response = self.create_key(name="Terceira")

        assert rejected_response.status_code == 409
        assert rejected_response.json()["code"] == "active_api_key_limit"
        assert revoke_response.status_code == 200
        assert revoke_response.json()["api_key"]["status"] == "REVOKED"
        assert accepted_response.status_code == 201

    def test_rotation_returns_only_a_new_secret_and_keeps_old_key_for_overlap(self) -> None:
        self.client.force_login(self.owner)
        original_response = self.create_key()
        original_id = original_response.json()["api_key"]["id"]

        response = self.post_json(
            f"{self.collection_url}{original_id}/rotate/",
            {"overlap_seconds": 600},
        )
        original = ApiKey.objects.get(id=original_id)

        assert response.status_code == 201
        assert response.json()["secret"].startswith("esk_")
        assert response.json()["api_key"]["id"] != original_id
        assert response.json()["previous_api_key"]["id"] == original_id
        assert response.json()["previous_api_key"]["expires_at"] is not None
        assert original.expires_at is not None
        assert original.expires_at <= timezone.now() + timedelta(seconds=601)
        assert ApiKey.objects.count() == 2

    def test_viewer_cannot_manage_organization_api_keys(self) -> None:
        self.client.force_login(self.viewer)

        response = self.create_key()

        assert response.status_code == 403
        assert response.json()["code"] == "organization_role_forbidden"

    @patch(
        "escrow.integrations.authentication.check_api_key_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_bearer_key_enforces_scope_and_uses_its_organization_tenant(
        self,
        _: object,
    ) -> None:
        self.client.force_login(self.owner)
        created = self.create_key().json()
        self.client.logout()

        response = self.client.get(
            "/api/v1/integrations/organization/",
            headers={"Authorization": f"Bearer {created['secret']}"},
        )

        assert response.status_code == 200
        assert response.json()["organization"]["id"] == str(self.organization.id)
        stored_key = ApiKey.objects.get(id=created["api_key"]["id"])
        assert stored_key.last_used_at is not None
        assert stored_key.last_used_ip == "127.0.0.1"

    @patch(
        "escrow.integrations.authentication.check_api_key_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_bearer_key_is_rejected_when_scope_does_not_permit_operation(
        self,
        limiter: MagicMock,
    ) -> None:
        self.client.force_login(self.owner)
        created = self.create_key(scopes=["webhooks:manage"]).json()
        self.client.logout()

        response = self.client.get(
            "/api/v1/integrations/organization/",
            headers={"Authorization": f"Bearer {created['secret']}"},
        )

        assert response.status_code == 403
        assert response.json()["code"] == "api_key_scope_forbidden"
        limiter.assert_called_once()

    @patch(
        "escrow.integrations.authentication.check_api_key_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_key_cannot_authenticate_a_deactivated_organization(self, _: object) -> None:
        self.client.force_login(self.owner)
        created = self.create_key().json()
        self.organization.is_active = False
        self.organization.save(update_fields=["is_active"])
        self.client.logout()

        response = self.client.get(
            "/api/v1/integrations/organization/",
            headers={"Authorization": f"Bearer {created['secret']}"},
        )

        assert response.status_code == 401
        assert response.json()["code"] == "api_key_invalid"

    @patch(
        "escrow.integrations.authentication.check_api_key_rate_limit",
        return_value=RateLimitDecision(allowed=False, retry_after_seconds=17),
    )
    def test_rate_limited_key_receives_retry_after_without_key_leakage(self, _: object) -> None:
        self.client.force_login(self.owner)
        created = self.create_key().json()
        self.client.logout()

        response = self.client.get(
            "/api/v1/integrations/organization/",
            headers={"Authorization": f"Bearer {created['secret']}"},
        )

        assert response.status_code == 429
        assert response["Retry-After"] == "17"
        assert response.json()["code"] == "api_key_rate_limited"
        assert created["secret"] not in response.content.decode()

    def test_query_parameter_never_authenticates_an_api_key(self) -> None:
        self.client.force_login(self.owner)
        created = self.create_key().json()
        self.client.logout()

        response = self.client.get(
            f"/api/v1/integrations/organization/?api_key={created['secret']}"
        )

        assert response.status_code == 401
        assert response.json()["code"] == "api_key_required"

    def test_errors_have_a_stable_correlation_contract(self) -> None:
        response = self.client.get(
            "/api/v1/integrations/organization/",
            headers={"X-Correlation-ID": "portfolio-contract-0001"},
        )

        body = response.json()
        assert response.status_code == 401
        assert body["code"] == "api_key_required"
        assert isinstance(body["message"], str)
        assert body["details"] == {}
        assert body["correlation_id"] == "portfolio-contract-0001"
        assert response["X-Correlation-ID"] == "portfolio-contract-0001"

    def test_drf_method_errors_keep_the_stable_correlation_contract(self) -> None:
        response = self.client.post(
            "/api/v1/integrations/organization/",
            data="{}",
            content_type="application/json",
            headers={"X-Correlation-ID": "portfolio-contract-0002"},
        )

        assert response.status_code == 405
        assert response.json()["code"] == "method_not_allowed"
        assert isinstance(response.json()["message"], str)
        assert response.json()["details"]
        assert response.json()["correlation_id"] == "portfolio-contract-0002"

    def test_rotation_accepts_an_empty_body_and_uses_the_default_overlap(self) -> None:
        self.client.force_login(self.owner)
        original_id = self.create_key().json()["api_key"]["id"]

        response = self.client.post(
            f"{self.collection_url}{original_id}/rotate/",
            data=b"",
            content_type="application/json",
        )

        assert response.status_code == 201
        assert response.json()["previous_api_key"]["id"] == original_id

    def test_rotation_honors_an_explicit_zero_second_overlap(self) -> None:
        self.client.force_login(self.owner)
        original_id = self.create_key().json()["api_key"]["id"]

        response = self.post_json(
            f"{self.collection_url}{original_id}/rotate/",
            {"overlap_seconds": 0},
        )
        original = ApiKey.objects.get(id=original_id)

        assert response.status_code == 201
        assert original.expires_at is not None
        assert original.expires_at <= timezone.now()

    def test_generated_openapi_describes_bearer_api_key_authentication(self) -> None:
        response = self.client.get("/api/v1/openapi.json")

        schema = response.json()
        operation = schema["paths"]["/api/v1/integrations/organization/"]["get"]
        parameters = operation["parameters"]

        assert response.status_code == 200
        assert schema["components"]["securitySchemes"]["ApiKeyAuth"] == {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "escrow API key",
        }
        assert any(parameter["name"] == "Authorization" for parameter in parameters)
        assert operation["security"] == [{"ApiKeyAuth": []}]
