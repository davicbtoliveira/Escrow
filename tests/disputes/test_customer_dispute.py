from __future__ import annotations

import json
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.pii import CustomerIdentity, envelope_cipher
from escrow.agreements.services import checkout_token_hash, customer_pii_context
from escrow.delivery.models import CustomerOtpChallenge
from escrow.delivery.services import report_delivery
from escrow.integrations.rate_limit import RateLimitDecision
from escrow.organizations.models import Organization


class CustomerDisputeOtpApiTests(TestCase):
    def setUp(self) -> None:
        organization = Organization.objects.create(name="Loja disputa otp")
        self.checkout_token = "chk_customer-dispute-capability"
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
            external_customer_id="buyer-dispute-001",
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
            idempotency_key="delivery-report-dispute-001",
            correlation_id="customer-dispute-test",
        )

    @patch(
        "escrow.disputes.views.check_customer_otp_verify_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch(
        "escrow.disputes.views.check_customer_otp_send_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch("escrow.delivery.services.send_customer_dispute_otp")
    @patch("escrow.delivery.services._new_otp_code", return_value="123456")
    def test_dispute_otp_verification_returns_a_dispute_scoped_capability(
        self,
        _: object,
        send_email: object,
        __: object,
        ___: object,
    ) -> None:
        otp_path = f"/api/v1/checkout/{self.checkout_token}/disputes/otp/"

        requested = self.client.post(otp_path, data=json.dumps({}), content_type="application/json")

        assert requested.status_code == 202
        assert set(requested.json()) == {"challenge_id", "expires_at"}
        challenge_id = requested.json()["challenge_id"]
        send_email.assert_called_once_with("buyer@example.test", "123456")  # type: ignore[attr-defined]
        assert "123456" not in requested.content.decode()
        challenge = CustomerOtpChallenge.objects.get(id=challenge_id)
        assert challenge.purpose == CustomerOtpChallenge.Purpose.DISPUTE

        verified = self.client.post(
            f"{otp_path}{challenge_id}/verify/",
            data=json.dumps({"code": "123456"}),
            content_type="application/json",
        )

        assert verified.status_code == 200
        dispute_token = verified.json()["dispute_token"]
        assert dispute_token.startswith("otp_dispute_")
