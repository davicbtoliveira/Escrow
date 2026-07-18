from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.integrations.key_service import create_api_key
from escrow.integrations.rate_limit import RateLimitDecision
from escrow.organizations.models import Organization


class DeliveryReportingApiTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Entrega teste")
        self.agreement = EscrowAgreement.objects.create(
            organization=self.organization,
            external_customer_id="buyer-delivery-001",
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
            checkout_token_hash=f"checkout-{uuid4().hex}",
            amount_minor=50_000,
            currency="BRL",
            fee_bps=200,
            delivery_window_days=7,
            status=EscrowAgreement.Status.HELD,
        )
        _, self.api_key = create_api_key(
            self.organization,
            name="Delivery reporter",
            scopes=["agreements:write"],
        )

    @patch(
        "escrow.integrations.authentication.check_api_key_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_authorized_delivery_report_starts_a_seven_calendar_day_inspection_once(
        self, _: object
    ) -> None:
        reported_at = timezone.now()
        path = f"/api/v1/agreements/{self.agreement.id}/delivery/"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Idempotency-Key": "delivery-report-001",
        }

        with patch("escrow.delivery.services.timezone.now", return_value=reported_at):
            created = self.client.post(
                path, data=json.dumps({}), content_type="application/json", headers=headers
            )
            replay = self.client.post(
                path, data=json.dumps({}), content_type="application/json", headers=headers
            )

        assert created.status_code == 202
        assert replay.status_code == 202
        assert created.json() == replay.json()
        assert created.json()["agreement_id"] == str(self.agreement.id)
        assert created.json()["status"] == "INSPECTION"
        assert created.json()["inspection_deadline_at"] == (
            reported_at + timedelta(days=7)
        ).isoformat().replace("+00:00", "Z")

        self.agreement.refresh_from_db()
        assert self.agreement.status == EscrowAgreement.Status.INSPECTION
        assert self.agreement.inspection_deadline_at == reported_at + timedelta(days=7)
        assert self.agreement.realtime_sequence == 1

