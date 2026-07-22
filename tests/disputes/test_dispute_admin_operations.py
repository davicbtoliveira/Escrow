from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.pii import CustomerIdentity, envelope_cipher
from escrow.agreements.services import customer_pii_context
from escrow.audit.models import AuditEvent
from escrow.disputes.models import Dispute, DisputeAdminDecision
from escrow.disputes.services import (
    open_dispute_after_customer_authorization,
    submit_dispute_recommendation,
)
from escrow.ledger.models import LedgerEntry, LedgerTransaction
from escrow.messaging.models import OutboxEvent
from escrow.organizations.models import Organization
from escrow.payments.models import Transfer
from escrow.risk.services import PLATFORM_ADMIN_GROUP, RISK_DISPUTE_ANALYST_GROUP, evaluate_dispute_risk_service


def _create_user_with_group(email: str, group_name: str) -> object:
    User = get_user_model()
    user = User.objects.create_user(
        email=email,
        password="Password123!",
        is_staff=True,
    )
    group, _ = Group.objects.get_or_create(name=group_name)
    user.groups.add(group)
    return user


def _setup_dispute_in_admin_review() -> tuple[EscrowAgreement, Dispute, object, object]:
    organization = Organization.objects.create(name="Loja Admin Resolve")
    now = timezone.now()
    agreement_id = uuid4()
    customer = CustomerIdentity(
        name="Carlos Cliente",
        email="carlos@example.test",
        document="12345678901",
        document_kind="CPF",
    )
    encrypted = envelope_cipher().encrypt(
        customer.plaintext(),
        customer_pii_context(organization.id, agreement_id),
    )
    agreement = EscrowAgreement.objects.create(
        id=agreement_id,
        organization=organization,
        external_customer_id="carlos-001",
        customer_name_masked="Carlos C.",
        customer_email_masked="c***@example.test",
        customer_document_masked="***.***.***-01",
        customer_document_kind="CPF",
        customer_email_blind_index="c" * 64,
        customer_document_blind_index="d" * 64,
        customer_pii_ciphertext=encrypted.ciphertext,
        customer_pii_nonce=encrypted.nonce,
        customer_pii_encrypted_data_key=encrypted.encrypted_data_key,
        customer_pii_kms_key_id=encrypted.kms_key_id,
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
        correlation_id="corr-admin-setup",
        now=now - timedelta(hours=10),
    )
    dispute = open_res.dispute
    report = evaluate_dispute_risk_service(dispute.id, now=now - timedelta(hours=9))

    analyst = _create_user_with_group("analyst-1@risk.test", RISK_DISPUTE_ANALYST_GROUP)
    submit_dispute_recommendation(
        dispute_id=dispute.id,
        analyst=analyst,
        recommendation="REFUND_TO_CUSTOMER",
        command_id=f"cmd-rec-{uuid4().hex}",
        rationale="Recomendação de reembolso pelo analista.",
        correlation_id="corr-rec-setup",
        now=now - timedelta(hours=5),
    )

    admin = _create_user_with_group("admin-1@risk.test", PLATFORM_ADMIN_GROUP)
    dispute.refresh_from_db()
    return agreement, dispute, analyst, admin


class DisputeAdminOperationsTests(TestCase):
    def test_admin_dashboard_shows_awaiting_decision_and_sla_counts(self) -> None:
        agreement, dispute, analyst, admin = _setup_dispute_in_admin_review()

        self.client.force_login(admin)
        response = self.client.get("/api/v1/operations/disputes/admin-dashboard/")

        assert response.status_code == 200
        data = response.json()
        assert data["counts"]["open"] == 1
        assert data["counts"]["awaiting_admin_decision"] == 1
        assert len(data["queue"]) == 1
        item = data["queue"][0]
        assert item["dispute_id"] == str(dispute.id)
        assert item["recommendation"]["recommendation"] == "REFUND_TO_CUSTOMER"

    def test_platform_admin_decrypts_customer_pii_with_audit_reason(self) -> None:
        agreement, dispute, analyst, admin = _setup_dispute_in_admin_review()

        self.client.force_login(admin)
        response = self.client.post(
            f"/api/v1/operations/disputes/{dispute.id}/decrypt-pii/",
            data=json.dumps({"reason": "Investigação legal de fraude"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        customer = response.json()["customer"]
        assert customer["name"] == "Carlos Cliente"
        assert customer["email"] == "carlos@example.test"
        assert customer["document"] == "12345678901"

        audit = AuditEvent.objects.get(event_type="dispute_customer_pii_decrypted")
        assert audit.payload["reason"] == "Investigação legal de fraude"

    def test_analyst_who_recommended_cannot_approve_as_admin_separation_of_duties(self) -> None:
        agreement, dispute, analyst, _ = _setup_dispute_in_admin_review()
        # Add PLATFORM_ADMIN group to analyst to attempt dual role
        admin_group, _ = Group.objects.get_or_create(name=PLATFORM_ADMIN_GROUP)
        analyst.groups.add(admin_group)

        self.client.force_login(analyst)
        response = self.client.post(
            f"/api/v1/operations/disputes/{dispute.id}/resolve/",
            data=json.dumps({
                "decision": "REFUND_TO_CUSTOMER",
                "rationale": "Tentando aprovar recomendação própria",
            }),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="cmd-dual-role-001",
        )

        assert response.status_code == 409
        assert response.json()["code"] == "dispute_resolution_conflict"

    def test_admin_approves_release_posting_balanced_ledger_and_terminal_state(self) -> None:
        agreement, dispute, analyst, admin = _setup_dispute_in_admin_review()

        self.client.force_login(admin)
        response = self.client.post(
            f"/api/v1/operations/disputes/{dispute.id}/resolve/",
            data=json.dumps({
                "decision": "RELEASE_TO_ORGANIZATION",
                "rationale": "Evidência de entrega aceita. Liberação de fundos autorizada.",
            }),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="cmd-admin-release-001",
        )

        assert response.status_code == 202
        body = response.json()["decision"]
        assert body["decision"] == "RELEASE_TO_ORGANIZATION"
        assert body["replayed"] is False

        dispute.refresh_from_db()
        agreement.refresh_from_db()
        assert dispute.status == Dispute.Status.RESOLVED
        assert agreement.status == EscrowAgreement.Status.RELEASED

        admin_dec = DisputeAdminDecision.objects.get(dispute=dispute)
        assert admin_dec.decision == "RELEASE_TO_ORGANIZATION"

        # Verify balanced ledger entries
        release_tx = LedgerTransaction.objects.get(kind=LedgerTransaction.Kind.FUNDS_RELEASED)
        entries = LedgerEntry.objects.filter(ledger_transaction=release_tx)
        # Gross = 100_000, Fee 2% = 2_000, Net = 98_000
        assert set(entries.values_list("account__code", "debit_minor", "credit_minor")) == {
            ("ESCROW_LIABILITY", 100_000, 0),
            ("ORGANIZATION_PAYABLE", 0, 98_000),
            ("PLATFORM_FEE_REVENUE", 0, 2_000),
        }

    def test_admin_approves_refund_posting_balanced_ledger_and_terminal_state(self) -> None:
        agreement, dispute, analyst, admin = _setup_dispute_in_admin_review()

        self.client.force_login(admin)
        response = self.client.post(
            f"/api/v1/operations/disputes/{dispute.id}/resolve/",
            data=json.dumps({
                "decision": "REFUND_TO_CUSTOMER",
                "rationale": "Reembolso ao cliente aprovado por extravio.",
            }),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="cmd-admin-refund-001",
        )

        assert response.status_code == 202
        body = response.json()["decision"]
        assert body["decision"] == "REFUND_TO_CUSTOMER"

        dispute.refresh_from_db()
        agreement.refresh_from_db()
        assert dispute.status == Dispute.Status.RESOLVED
        assert agreement.status == EscrowAgreement.Status.REFUNDED

        # Verify balanced ledger entries
        refund_tx = LedgerTransaction.objects.get(kind=LedgerTransaction.Kind.FUNDS_REFUNDED)
        entries = LedgerEntry.objects.filter(ledger_transaction=refund_tx)
        assert set(entries.values_list("account__code", "debit_minor", "credit_minor")) == {
            ("ESCROW_LIABILITY", 100_000, 0),
            ("PIX_CLEARING", 0, 100_000),
        }
