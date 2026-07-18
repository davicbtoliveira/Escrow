from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.organizations.models import Organization
from escrow.payments.models import Transfer
from escrow.risk.models import FundingRiskReview
from escrow.risk.services import (
    PLATFORM_ADMIN_GROUP,
    RISK_DISPUTE_ANALYST_GROUP,
    evaluate_funding_transfer,
)


class FundingReviewApiTests(TestCase):
    def setUp(self) -> None:
        organization = Organization.objects.create(name="API risk review organization")
        Organization.objects.filter(id=organization.id).update(
            created_at=timezone.now() - timedelta(days=6)
        )
        self.agreement = EscrowAgreement.objects.create(
            organization=organization,
            external_customer_id="api-review-buyer",
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
            checkout_token_hash=uuid4().hex * 2,
            amount_minor=5_000_000,
            currency="BRL",
            fee_bps=200,
            delivery_window_days=7,
            status=EscrowAgreement.Status.REVIEW_REQUIRED,
        )
        self.transfer = Transfer.objects.create(
            agreement=self.agreement,
            kind=Transfer.Kind.FUNDING,
            status=Transfer.Status.PROCESSING,
            amount_minor=self.agreement.amount_minor,
            currency=self.agreement.currency,
            provider=Transfer.Provider.SANDBOX_PIX,
            provider_reference="api-risk-review-pix",
            idempotency_key="api-risk-review-funding",
        )
        self.decision = evaluate_funding_transfer(self.transfer.id, now=timezone.now())
        self.analyst = get_user_model().objects.create_user(
            email="analyst@risk-api.test",
            password="Uma senha forte e exclusiva 2026!",
            is_staff=True,
        )
        analyst_group, _ = Group.objects.get_or_create(name=RISK_DISPUTE_ANALYST_GROUP)
        self.analyst.groups.add(analyst_group)
        self.queue_url = "/api/v1/operations/risk/funding-reviews/"

    def test_analyst_sees_only_masked_manual_queue_data(self) -> None:
        self.client.force_login(self.analyst)

        response = self.client.get(self.queue_url)

        assert response.status_code == 200
        item = response.json()["reviews"][0]
        assert item["decision_id"] == str(self.decision.id)
        assert item["customer"]["name"] == "A***"
        assert item["customer"]["email_masked"] == "a***@example.test"
        assert "customer_pii_ciphertext" not in response.content.decode()
        assert item["policy_version"] == self.decision.policy_version

    def test_analyst_resolution_requires_idempotency_and_enqueues_result(self) -> None:
        self.client.force_login(self.analyst)
        url = f"{self.queue_url}{self.decision.id}/resolve/"
        payload = {"outcome": "APPROVED", "rationale": "Análise documentada e suficiente."}

        missing = self.client.post(url, data=json.dumps(payload), content_type="application/json")
        accepted = self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            headers={"Idempotency-Key": "risk-api-review-command-001"},
        )

        assert missing.status_code == 400
        assert missing.json()["code"] == "idempotency_key_required"
        assert accepted.status_code == 202
        assert accepted.json()["review"]["outcome"] == "APPROVED"
        assert FundingRiskReview.objects.filter(decision=self.decision).count() == 1

    def test_platform_admin_cannot_bypass_risk_analyst_role(self) -> None:
        admin = get_user_model().objects.create_user(
            email="admin@risk-api.test",
            password="Uma senha forte e exclusiva 2026!",
            is_staff=True,
        )
        group, _ = Group.objects.get_or_create(name=PLATFORM_ADMIN_GROUP)
        admin.groups.add(group)
        self.client.force_login(admin)

        response = self.client.get(self.queue_url)

        assert response.status_code == 403
        assert response.json()["code"] == "risk_analyst_required"
