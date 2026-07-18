from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.pii import CustomerIdentity, envelope_cipher
from escrow.agreements.services import checkout_token_hash, customer_pii_context
from escrow.delivery.services import report_delivery
from escrow.integrations.rate_limit import RateLimitDecision
from escrow.messaging.models import OutboxEvent
from escrow.organizations.models import Organization
from escrow.payments.models import Transfer


class CustomerAcceptanceApiTests(TestCase):
    def setUp(self) -> None:
        organization = Organization.objects.create(name="Aceite externo teste")
        self.checkout_token = "chk_delivery-acceptance-capability"
        agreement_id = uuid4()
        customer = CustomerIdentity(
            name="Ana Compradora",
            email="buyer@example.test",
            document="52998224725",
            document_kind="CPF",
        )
        encrypted = envelope_cipher().encrypt(
            customer.plaintext(),
            customer_pii_context(organization.id, agreement_id),
        )
        self.agreement = EscrowAgreement.objects.create(
            id=agreement_id,
            organization=organization,
            external_customer_id="buyer-acceptance-001",
            customer_name_masked="Ana C.",
            customer_email_masked="b***@example.test",
            customer_document_masked="***.***.***-25",
            customer_document_kind="CPF",
            customer_email_blind_index="a" * 64,
            customer_document_blind_index="b" * 64,
            customer_pii_ciphertext=encrypted.ciphertext,
            customer_pii_nonce=encrypted.nonce,
            customer_pii_encrypted_data_key=encrypted.encrypted_data_key,
            customer_pii_kms_key_id=encrypted.kms_key_id,
            checkout_token_hash=checkout_token_hash(self.checkout_token),
            amount_minor=50_000,
            currency="BRL",
            fee_bps=200,
            delivery_window_days=7,
            status=EscrowAgreement.Status.HELD,
        )
        report_delivery(
            organization_id=organization.id,
            agreement_id=self.agreement.id,
            idempotency_key="delivery-report-acceptance-001",
            correlation_id="delivery-acceptance-test",
        )

    @patch(
        "escrow.delivery.views.check_customer_otp_verify_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch(
        "escrow.delivery.views.check_customer_otp_send_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch("escrow.delivery.services.send_customer_acceptance_otp")
    @patch("escrow.delivery.services._new_otp_code", return_value="123456")
    def test_verified_customer_acceptance_enqueues_one_release_without_exposing_the_otp(
        self,
        _: object,
        send_email: object,
        __: object,
        ___: object,
    ) -> None:
        otp_path = f"/api/v1/checkout/{self.checkout_token}/delivery-acceptance/otp/"

        requested = self.client.post(otp_path, data=json.dumps({}), content_type="application/json")

        assert requested.status_code == 202
        assert set(requested.json()) == {"challenge_id", "expires_at"}
        challenge_id = requested.json()["challenge_id"]
        send_email.assert_called_once_with("buyer@example.test", "123456")  # type: ignore[attr-defined]
        assert "123456" not in requested.content.decode()

        verified = self.client.post(
            f"{otp_path}{challenge_id}/verify/",
            data=json.dumps({"code": "123456"}),
            content_type="application/json",
        )

        assert verified.status_code == 200
        acceptance_token = verified.json()["acceptance_token"]
        assert acceptance_token.startswith("otp_accept_")

        accepted = self.client.post(
            f"/api/v1/checkout/{self.checkout_token}/delivery-acceptance/",
            data=json.dumps(
                {"challenge_id": challenge_id, "acceptance_token": acceptance_token}
            ),
            content_type="application/json",
        )
        replayed = self.client.post(
            f"/api/v1/checkout/{self.checkout_token}/delivery-acceptance/",
            data=json.dumps(
                {"challenge_id": challenge_id, "acceptance_token": acceptance_token}
            ),
            content_type="application/json",
        )

        assert accepted.status_code == 202
        assert replayed.status_code == 202
        assert replayed.json() == accepted.json()
        assert accepted.json()["status"] == "PROCESSING"
        assert Transfer.objects.filter(kind=Transfer.Kind.RELEASE).count() == 1
        assert OutboxEvent.objects.filter(message_type="ReleaseFunds.v1").count() == 1
        self.agreement.refresh_from_db()
        assert self.agreement.status == EscrowAgreement.Status.RELEASE_PENDING
        assert self.agreement.inspection_deadline_at is not None
        assert self.agreement.inspection_deadline_at > timezone.now() - timedelta(days=1)

    def test_invalid_otp_or_acceptance_proof_cannot_enqueue_a_release(self) -> None:
        otp_path = f"/api/v1/checkout/{self.checkout_token}/delivery-acceptance/otp/"
        allowed = RateLimitDecision(allowed=True, retry_after_seconds=0)
        with (
            patch("escrow.delivery.views.check_customer_otp_send_rate_limit", return_value=allowed),
            patch(
                "escrow.delivery.views.check_customer_otp_verify_rate_limit",
                return_value=allowed,
            ),
            patch("escrow.delivery.services.send_customer_acceptance_otp"),
            patch("escrow.delivery.services._new_otp_code", return_value="123456"),
        ):
            requested = self.client.post(
                otp_path,
                data=json.dumps({}),
                content_type="application/json",
            )
            challenge_id = requested.json()["challenge_id"]
            invalid = self.client.post(
                f"{otp_path}{challenge_id}/verify/",
                data=json.dumps({"code": "000000"}),
                content_type="application/json",
            )
            verified = self.client.post(
                f"{otp_path}{challenge_id}/verify/",
                data=json.dumps({"code": "123456"}),
                content_type="application/json",
            )
            unauthorized = self.client.post(
                f"/api/v1/checkout/{self.checkout_token}/delivery-acceptance/",
                data=json.dumps(
                    {
                        "challenge_id": challenge_id,
                        "acceptance_token": "otp_accept_not-the-verified-proof",
                    }
                ),
                content_type="application/json",
            )

        assert invalid.status_code == 409
        assert verified.status_code == 200
        assert unauthorized.status_code == 403
        assert Transfer.objects.filter(kind=Transfer.Kind.RELEASE).count() == 0
        assert OutboxEvent.objects.filter(message_type="ReleaseFunds.v1").count() == 0
