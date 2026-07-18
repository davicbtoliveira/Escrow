from __future__ import annotations

import json
import re
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase, override_settings

from escrow.organizations.models import OrganizationMember


class AuthenticationApiTests(TestCase):
    registration_url = "/api/v1/auth/register/"
    password = "Uma senha forte e exclusiva 2026!"

    def registration_payload(self, **overrides: str) -> dict[str, str]:
        payload = {
            "email": "proprietaria@acme.example",
            "password": self.password,
            "password_confirmation": self.password,
            "organization_name": "Acme Comércio LTDA",
        }
        payload.update(overrides)
        return payload

    def post_json(self, client: Client, path: str, payload: dict[str, str], **headers: str):
        return client.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            headers=headers,
        )

    @override_settings(HIBP_MODE="mock", HIBP_MOCK_PWNED_PASSWORDS="")
    def test_registration_creates_owner_and_organization_atomically(self) -> None:
        response = self.post_json(self.client, self.registration_url, self.registration_payload())

        assert response.status_code == 201
        assert response.json()["user"]["email"] == "proprietaria@acme.example"
        user = get_user_model().objects.get(email="proprietaria@acme.example")
        membership = OrganizationMember.objects.get(user=user)
        assert membership.role == OrganizationMember.Role.OWNER
        assert membership.organization.name == "Acme Comércio LTDA"
        assert "_auth_user_id" in self.client.session

    @override_settings(HIBP_MODE="mock", HIBP_MOCK_PWNED_PASSWORDS="")
    def test_registration_rejects_mismatched_password_confirmation(self) -> None:
        response = self.post_json(
            self.client,
            self.registration_url,
            self.registration_payload(password_confirmation="Outra senha forte e exclusiva 2026!"),
        )

        assert response.status_code == 400
        assert response.json()["errors"]["password_confirmation"] == ["As senhas não coincidem."]
        assert get_user_model().objects.count() == 0

    @override_settings(
        HIBP_MODE="mock",
        HIBP_MOCK_PWNED_PASSWORDS="Uma senha forte e exclusiva 2026!",
    )
    def test_registration_rejects_a_deterministically_pwned_password_in_development(self) -> None:
        response = self.post_json(self.client, self.registration_url, self.registration_payload())

        assert response.status_code == 400
        assert response.json()["errors"]["password"] == [
            "Esta senha aparece em vazamentos conhecidos. Escolha outra."
        ]
        assert get_user_model().objects.count() == 0

    @override_settings(HIBP_MODE="live")
    @patch("escrow.identity.hibp.urlopen", side_effect=URLError("offline"))
    def test_registration_fails_closed_when_live_hibp_is_unavailable(self, _: object) -> None:
        response = self.post_json(self.client, self.registration_url, self.registration_payload())

        assert response.status_code == 503
        assert response.json()["errors"]["password"] == [
            "Não foi possível validar a senha contra vazamentos. Tente novamente."
        ]
        assert get_user_model().objects.count() == 0

    @override_settings(HIBP_MODE="mock", HIBP_MOCK_PWNED_PASSWORDS="")
    def test_login_and_logout_require_csrf_and_use_a_server_session(self) -> None:
        self.post_json(self.client, self.registration_url, self.registration_payload())
        csrf_client = Client(enforce_csrf_checks=True)

        csrf_response = csrf_client.get("/api/v1/auth/csrf/")
        token = csrf_response.json()["csrfToken"]
        rejected_response = self.post_json(
            csrf_client,
            "/api/v1/auth/login/",
            {"email": "proprietaria@acme.example", "password": self.password},
        )
        accepted_response = self.post_json(
            csrf_client,
            "/api/v1/auth/login/",
            {"email": "proprietaria@acme.example", "password": self.password},
            **{"X-CSRFToken": token},
        )

        assert rejected_response.status_code == 403
        assert accepted_response.status_code == 200
        assert "_auth_user_id" in csrf_client.session
        assert csrf_client.cookies["escrow_session"]["httponly"]
        refreshed_token = csrf_client.get("/api/v1/auth/csrf/").json()["csrfToken"]

        logout_response = self.post_json(
            csrf_client,
            "/api/v1/auth/logout/",
            {},
            **{"X-CSRFToken": refreshed_token},
        )

        assert logout_response.status_code == 204
        assert "_auth_user_id" not in csrf_client.session

    @override_settings(
        HIBP_MODE="mock", HIBP_MOCK_PWNED_PASSWORDS="", EMAIL_DELIVERY_BACKEND="django"
    )
    def test_password_recovery_sends_an_email_without_returning_the_reset_secret(self) -> None:
        self.post_json(self.client, self.registration_url, self.registration_payload())

        request_response = self.post_json(
            self.client,
            "/api/v1/auth/password-recovery/",
            {"email": "proprietaria@acme.example"},
        )

        assert request_response.status_code == 202
        assert request_response.json() == {"status": "accepted"}
        assert len(mail.outbox) == 1
        assert "Uma senha forte" not in mail.outbox[0].body

        reset_url = re.search(r"https?://\S+", mail.outbox[0].body)
        assert reset_url is not None
        query = parse_qs(urlparse(reset_url.group()).query)
        confirm_response = self.post_json(
            self.client,
            "/api/v1/auth/password-recovery/confirm/",
            {
                "uid": query["uid"][0],
                "token": query["token"][0],
                "password": "Uma nova senha forte e exclusiva 2026!",
                "password_confirmation": "Uma nova senha forte e exclusiva 2026!",
            },
        )

        assert confirm_response.status_code == 200
        user = get_user_model().objects.get(email="proprietaria@acme.example")
        assert user.check_password("Uma nova senha forte e exclusiva 2026!")
