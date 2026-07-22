from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.audit.models import AuditEvent
from escrow.disputes.models import Dispute, DisputeRecommendation
from escrow.disputes.services import open_dispute_after_customer_authorization
from escrow.messaging.models import OutboxEvent
from escrow.organizations.models import Organization
from escrow.risk.models import DisputeRiskPolicy, DisputeRiskReport
from escrow.risk.services import RISK_DISPUTE_ANALYST_GROUP, evaluate_dispute_risk_service


def _create_analyst_user(email: str = "analyst@risk.test") -> object:
    User = get_user_model()
    analyst = User.objects.create_user(
        email=email,
        password="Password123!",
        is_staff=True,
    )
    group, _ = Group.objects.get_or_create(name=RISK_DISPUTE_ANALYST_GROUP)
    analyst.groups.add(group)
    return analyst


def _create_dispute_with_report(*, opened_ago: timedelta = timedelta(hours=10)) -> tuple[EscrowAgreement, Dispute, DisputeRiskReport]:
    organization = Organization.objects.create(name="Loja Operacoes Disputa")
    now = timezone.now()
    opened_at = now - opened_ago
    agreement = EscrowAgreement.objects.create(
        organization=organization,
        external_customer_id=f"cust-{uuid4().hex}",
        customer_name_masked="Ana C.",
        customer_email_masked="a***@example.test",
        customer_document_masked="***.***.***-01",
        customer_document_kind="CPF",
        customer_email_blind_index="a" * 64,
        customer_document_blind_index="b" * 64,
        customer_pii_ciphertext=b"ciphertext",
        customer_pii_nonce=b"nonce",
        customer_pii_encrypted_data_key=b"key",
        customer_pii_kms_key_id="kms",
        checkout_token_hash=uuid4().hex * 2,
        amount_minor=100_000,
        currency="BRL",
        fee_bps=200,
        delivery_window_days=7,
        status=EscrowAgreement.Status.INSPECTION,
        inspection_deadline_at=now + timedelta(days=5),
    )
    open_res = open_dispute_after_customer_authorization(
        agreement_id=agreement.id,
        correlation_id=f"corr-{uuid4().hex}",
        now=opened_at,
    )
    dispute = open_res.dispute
    report = evaluate_dispute_risk_service(dispute.id, now=opened_at)
    return agreement, dispute, report


class DisputeAnalystDashboardApiTests(TestCase):
    def setUp(self) -> None:
        self.analyst = _create_analyst_user()
        self.dashboard_url = "/api/v1/operations/disputes/dashboard/"

    def test_unauthenticated_user_cannot_access_analyst_dashboard(self) -> None:
        response = self.client.get(self.dashboard_url)
        assert response.status_code == 401

    def test_non_analyst_user_cannot_access_analyst_dashboard(self) -> None:
        regular_user = get_user_model().objects.create_user(
            email="user@example.test", password="Password123!", is_staff=False
        )
        self.client.force_login(regular_user)
        response = self.client.get(self.dashboard_url)
        assert response.status_code == 403

    def test_analyst_dashboard_returns_sla_counts_and_masked_queues(self) -> None:
        # Create 1 on-track (opened 10h ago), 1 at-risk (opened 50h ago), 1 overdue (opened 80h ago)
        _create_dispute_with_report(opened_ago=timedelta(hours=10))
        _create_dispute_with_report(opened_ago=timedelta(hours=50))
        _create_dispute_with_report(opened_ago=timedelta(hours=80))

        self.client.force_login(self.analyst)
        response = self.client.get(self.dashboard_url)

        assert response.status_code == 200
        data = response.json()
        assert set(data) == {"counts", "queue"}
        counts = data["counts"]
        assert counts["ANALYST_REVIEW"] == 3
        assert counts["on_track"] == 1
        assert counts["at_risk"] == 1
        assert counts["overdue"] == 1

        queue = data["queue"]
        assert len(queue) == 3
        # Check PII masking
        for item in queue:
            assert "name_masked" in item["organization"]
            assert "email_masked" in item["customer"]
            assert "document_masked" in item["customer"]
            # Raw PII / secrets must NOT be present
            assert "pii_ciphertext" not in item["customer"]
            assert "api_key" not in item


class DisputeRecommendationApiTests(TestCase):
    def setUp(self) -> None:
        self.analyst = _create_analyst_user()

    def test_analyst_submits_release_recommendation_to_admin_review(self) -> None:
        agreement, dispute, report = _create_dispute_with_report()
        url = f"/api/v1/operations/disputes/{dispute.id}/recommendation/"
        payload = {
            "recommendation": "RELEASE_TO_ORGANIZATION",
            "rationale": "Evidência validada, cliente concorda com liberação parcial.",
        }

        self.client.force_login(self.analyst)
        response = self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="cmd-rec-release-001",
        )

        assert response.status_code == 202
        body = response.json()["recommendation"]
        assert body["dispute_id"] == str(dispute.id)
        assert body["recommendation"] == "RELEASE_TO_ORGANIZATION"
        assert body["replayed"] is False

        dispute.refresh_from_db()
        assert dispute.status == Dispute.Status.ADMIN_REVIEW
        rec = DisputeRecommendation.objects.get(dispute=dispute)
        assert rec.recommendation == "RELEASE_TO_ORGANIZATION"
        assert rec.command_id == "cmd-rec-release-001"

        # Audit event for recommendation submission
        audit = AuditEvent.objects.get(
            event_type="dispute_recommendation_submitted",
            agreement=agreement,
        )
        assert audit.payload["recommendation"] == "RELEASE_TO_ORGANIZATION"

    def test_analyst_recommendation_submission_is_idempotent(self) -> None:
        agreement, dispute, report = _create_dispute_with_report()
        url = f"/api/v1/operations/disputes/{dispute.id}/recommendation/"
        payload = {
            "recommendation": "REFUND_TO_CUSTOMER",
            "rationale": "Produto danificado comprovado por foto.",
        }

        self.client.force_login(self.analyst)
        res1 = self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="cmd-rec-refund-001",
        )
        res2 = self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="cmd-rec-refund-001",
        )

        assert res1.status_code == 202
        assert res1.json()["recommendation"]["replayed"] is False
        assert res2.status_code == 202
        assert res2.json()["recommendation"]["replayed"] is True

        assert DisputeRecommendation.objects.filter(dispute=dispute).count() == 1

    def test_second_different_recommendation_command_conflicts(self) -> None:
        agreement, dispute, report = _create_dispute_with_report()
        url = f"/api/v1/operations/disputes/{dispute.id}/recommendation/"
        payload = {
            "recommendation": "REFUND_TO_CUSTOMER",
            "rationale": "Razão válida.",
        }

        self.client.force_login(self.analyst)
        self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="cmd-rec-refund-002",
        )

        second_res = self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="cmd-rec-refund-003",
        )

        assert second_res.status_code == 409
        assert second_res.json()["code"] == "dispute_recommendation_conflict"
