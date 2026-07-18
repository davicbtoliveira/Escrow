from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.services import checkout_token_hash
from escrow.integrations.rate_limit import RateLimitDecision
from escrow.organizations.models import Organization


class PublicDeliveryTimingTests(TestCase):
    @patch(
        "escrow.agreements.views.check_public_checkout_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_checkout_exposes_the_inspection_deadline_without_customer_secrets(
        self, _: object
    ) -> None:
        organization = Organization.objects.create(name="Public delivery timing")
        checkout_token = "chk_public-delivery-timing"
        deadline = timezone.make_aware(datetime(2026, 7, 25, 14, 30))
        agreement = EscrowAgreement.objects.create(
            organization=organization,
            external_customer_id="public-delivery-buyer",
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
            checkout_token_hash=checkout_token_hash(checkout_token),
            amount_minor=50_000,
            currency="BRL",
            fee_bps=200,
            delivery_window_days=7,
            status=EscrowAgreement.Status.INSPECTION,
            inspection_deadline_at=deadline,
        )

        response = self.client.get(f"/api/v1/checkout/{checkout_token}/")

        assert response.status_code == 200
        payload = response.json()["agreement"]
        assert payload["id"] == str(agreement.id)
        assert payload["inspection_deadline_at"] == "2026-07-25T18:30:00Z"
        assert "external_customer_id" not in payload
        assert "customer_pii" not in response.content.decode()

