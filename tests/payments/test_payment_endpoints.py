from __future__ import annotations

import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.services import checkout_token_hash
from escrow.integrations.key_service import create_api_key
from escrow.integrations.rate_limit import RateLimitDecision
from escrow.messaging.models import OutboxEvent
from escrow.organizations.models import Organization
from escrow.payments.callbacks import sign_sandbox_callback
from escrow.payments.models import ProviderCallbackReceipt, SandboxPixCharge, Transfer
from escrow.payments.services import create_sandbox_pix_charge

_CALLBACK_SECRET = "payment-endpoint-callback-secret"


class SandboxPixEndpointTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Payment endpoint organization")
        self.checkout_token = "chk_payment-endpoint-capability"
        self.agreement = EscrowAgreement.objects.create(
            organization=self.organization,
            external_customer_id="payment-endpoint-buyer",
            customer_name_masked="A***",
            customer_email_masked="a***@example.test",
            customer_document_masked="***.***.***-25",
            customer_document_kind="CPF",
            customer_email_blind_index="a" * 64,
            customer_document_blind_index="b" * 64,
            customer_pii_ciphertext=b"ciphertext",
            customer_pii_nonce=b"nonce",
            customer_pii_encrypted_data_key=b"encrypted-key",
            customer_pii_kms_key_id="test-key",
            checkout_token_hash=checkout_token_hash(self.checkout_token),
            amount_minor=50_000,
            currency="BRL",
            fee_bps=200,
            delivery_window_days=7,
        )
        _, self.payments_api_key = create_api_key(
            self.organization,
            name="Sandbox payment control",
            scopes=["payments:write"],
        )

    @patch(
        "escrow.payments.views.check_public_checkout_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_public_checkout_creates_and_replays_a_safe_sandbox_pix_charge(self, _: object) -> None:
        path = f"/api/v1/checkout/{self.checkout_token}/pix-charges/"
        headers = {"Idempotency-Key": "checkout-pix-charge-001"}

        created = self.client.post(path, headers=headers)
        replay = self.client.post(path, headers=headers)

        assert created.status_code == 202
        assert replay.status_code == 202
        assert created.json() == replay.json()
        assert created["Cache-Control"] == "no-store, private"
        assert created["Referrer-Policy"] == "no-referrer"
        assert set(created.json()) == {"payment"}
        payment = created.json()["payment"]
        assert set(payment) == {"id", "status", "amount", "currency", "pix_copy_paste"}
        assert payment["status"] == "PENDING"
        assert payment["amount"] == "500.00"
        assert payment["currency"] == "BRL"
        assert payment["pix_copy_paste"].startswith("ESCROW-SANDBOX-PIX:")
        assert self.checkout_token not in created.content.decode()
        assert SandboxPixCharge.objects.count() == 1
        assert OutboxEvent.objects.filter(message_type="AgreementStatusChanged.v1").count() == 1

    @patch(
        "escrow.payments.views.check_public_checkout_rate_limit",
        return_value=RateLimitDecision(allowed=False, retry_after_seconds=12),
    )
    def test_public_pix_creation_honors_the_checkout_rate_limit(self, _: object) -> None:
        response = self.client.post(
            f"/api/v1/checkout/{self.checkout_token}/pix-charges/",
            headers={"Idempotency-Key": "checkout-pix-rate-limit-001"},
        )

        assert response.status_code == 429
        assert response.json()["code"] == "public_checkout_rate_limited"
        assert response["Retry-After"] == "12"
        assert self.checkout_token not in response.content.decode()

    @override_settings(SANDBOX_PIX_CALLBACK_SIGNING_SECRET=_CALLBACK_SECRET)
    @patch(
        "escrow.payments.views.check_webhook_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_verified_callback_is_accepted_once_and_places_the_risk_command_in_the_outbox(
        self,
        _: object,
    ) -> None:
        charge = create_sandbox_pix_charge(
            agreement_id=self.agreement.id,
            idempotency_key="payment-endpoint-callback-charge-001",
        ).charge
        now = timezone.now()
        timestamp = str(int(now.timestamp()))
        raw_body = json.dumps(
            {
                "event_id": "payment-endpoint-callback-001",
                "provider_reference": charge.provider_reference,
                "outcome": "CONFIRMED",
            },
            separators=(",", ":"),
        ).encode()
        headers = {
            "HTTP_X_SANDBOX_PIX_TIMESTAMP": timestamp,
            "HTTP_X_SANDBOX_PIX_SIGNATURE": sign_sandbox_callback(
                _CALLBACK_SECRET,
                timestamp,
                raw_body,
            ),
        }

        created = self.client.post(
            "/api/v1/providers/sandbox-pix/callbacks/",
            data=raw_body,
            content_type="application/json",
            **headers,
        )
        replay = self.client.post(
            "/api/v1/providers/sandbox-pix/callbacks/",
            data=raw_body,
            content_type="application/json",
            **headers,
        )

        assert created.status_code == 202
        assert replay.status_code == 202
        assert created.json() == {"status": "accepted", "duplicate": False}
        assert replay.json() == {"status": "accepted", "duplicate": True}
        assert ProviderCallbackReceipt.objects.count() == 1
        assert Transfer.objects.filter(status=Transfer.Status.PROCESSING).count() == 1
        assert OutboxEvent.objects.filter(message_type="EvaluateFundingRisk.v1").count() == 1

    @override_settings(SANDBOX_PIX_CALLBACK_SIGNING_SECRET=_CALLBACK_SECRET)
    @patch(
        "escrow.payments.views.check_webhook_rate_limit",
        return_value=RateLimitDecision(allowed=False, retry_after_seconds=9),
    )
    def test_provider_callback_honors_webhook_rate_limit(self, _: object) -> None:
        response = self.client.post(
            "/api/v1/providers/sandbox-pix/callbacks/",
            data=b"{}",
            content_type="application/json",
        )

        assert response.status_code == 429
        assert response.json()["code"] == "webhook_rate_limited"
        assert response["Retry-After"] == "9"
        assert ProviderCallbackReceipt.objects.count() == 0

    @override_settings(SANDBOX_PIX_CALLBACK_SIGNING_SECRET=_CALLBACK_SECRET)
    @patch(
        "escrow.integrations.authentication.check_api_key_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_organization_can_delay_confirm_and_repeat_its_own_sandbox_callback(
        self,
        _: object,
    ) -> None:
        charge = create_sandbox_pix_charge(
            agreement_id=self.agreement.id,
            idempotency_key="payment-endpoint-control-charge-001",
        ).charge
        path = f"/api/v1/sandbox/pix/charges/{charge.id}/actions/"
        headers = {"Authorization": f"Bearer {self.payments_api_key}"}

        delayed = self.client.post(
            path,
            data=json.dumps({"action": "delay"}),
            content_type="application/json",
            headers=headers,
        )
        confirmed = self.client.post(
            path,
            data=json.dumps({"action": "confirm"}),
            content_type="application/json",
            headers=headers,
        )
        duplicate = self.client.post(
            path,
            data=json.dumps({"action": "duplicate"}),
            content_type="application/json",
            headers=headers,
        )

        assert delayed.status_code == 202
        assert delayed.json()["delivery"] == "DELAYED"
        assert confirmed.status_code == 202
        assert confirmed.json() == {"status": "accepted", "duplicate": False}
        assert duplicate.status_code == 202
        assert duplicate.json() == {"status": "accepted", "duplicate": True}
        assert ProviderCallbackReceipt.objects.count() == 1
        assert OutboxEvent.objects.filter(message_type="EvaluateFundingRisk.v1").count() == 1
