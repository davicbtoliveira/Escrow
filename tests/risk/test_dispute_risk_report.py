from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.disputes.models import Dispute, Evidence
from escrow.disputes.services import open_dispute_after_customer_authorization
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.topology import RISK_DISPUTE_QUEUE
from escrow.organizations.models import Organization
from escrow.payments.models import Transfer
from escrow.risk.models import DisputeRiskReport
from escrow.risk.services import evaluate_dispute_risk_service
from escrow.risk.tasks import evaluate_dispute_risk


def _setup_disputed_agreement(*, customer_doc: str = "12345678901") -> tuple[EscrowAgreement, Dispute]:
    organization = Organization.objects.create(name="Loja Teste Risco Disputa")
    agreement = EscrowAgreement.objects.create(
        organization=organization,
        external_customer_id=f"cust-{uuid4().hex}",
        customer_name_masked="C***",
        customer_email_masked="c***@example.test",
        customer_document_masked="***.***.***-01",
        customer_document_kind="CPF",
        customer_email_blind_index=customer_doc * 2,
        customer_document_blind_index=customer_doc * 2,
        customer_pii_ciphertext=b"ciphertext",
        customer_pii_nonce=b"nonce",
        customer_pii_encrypted_data_key=b"encrypted-key",
        customer_pii_kms_key_id="key",
        checkout_token_hash=uuid4().hex * 2,
        amount_minor=100_000,
        currency="BRL",
        fee_bps=200,
        delivery_window_days=7,
        status=EscrowAgreement.Status.INSPECTION,
        inspection_deadline_at=timezone.now() + timedelta(days=5),
    )
    open_res = open_dispute_after_customer_authorization(
        agreement_id=agreement.id,
        correlation_id=f"corr-{uuid4().hex}",
    )
    return agreement, open_res.dispute


class DisputeRiskReportServiceTests(TestCase):
    def test_clean_dispute_generates_explicit_no_suspicion_report(self) -> None:
        agreement, dispute = _setup_disputed_agreement()

        report = evaluate_dispute_risk_service(dispute.id, now=timezone.now())

        dispute.refresh_from_db()
        assert report.suspicion_result == "NO_SUSPICION"
        assert report.score == 0
        assert report.flags == []
        assert "No suspicious risk indicators detected" in report.summary
        assert dispute.status == Dispute.Status.ANALYST_REVIEW
        assert Transfer.objects.filter(agreement=agreement).count() == 0

    def test_duplicate_evidence_triggers_suspicion_indicator(self) -> None:
        _, dispute1 = _setup_disputed_agreement()
        _, dispute2 = _setup_disputed_agreement()

        sha256_hash = "a" * 64
        Evidence.objects.create(
            dispute=dispute1,
            object_key="key1",
            extension="pdf",
            media_type="application/pdf",
            size_bytes=100,
            sha256=sha256_hash,
            uploaded_at=timezone.now(),
        )
        Evidence.objects.create(
            dispute=dispute2,
            object_key="key2",
            extension="pdf",
            media_type="application/pdf",
            size_bytes=100,
            sha256=sha256_hash,
            uploaded_at=timezone.now(),
        )

        report1 = evaluate_dispute_risk_service(dispute1.id, now=timezone.now())
        report2 = evaluate_dispute_risk_service(dispute2.id, now=timezone.now())

        assert report2.suspicion_result == "SUSPICIOUS_INDICATORS"
        assert "duplicate_evidence_detected" in report2.flags
        assert report2.score >= 30

    def test_worker_task_is_idempotent_and_does_not_move_funds(self) -> None:
        agreement, dispute = _setup_disputed_agreement()
        envelope = MessageEnvelope.build(
            message_id=uuid4(),
            message_type="EvaluateDisputeRisk.v1",
            version=1,
            occurred_at=timezone.now(),
            correlation_id="test-corr",
            causation_id=str(dispute.id),
            tenant_id=str(agreement.organization_id),
            payload={"agreement_id": str(agreement.id), "dispute_id": str(dispute.id)},
        )

        res1 = evaluate_dispute_risk.apply(args=[envelope.to_dict()]).get()
        res2 = evaluate_dispute_risk.apply(args=[envelope.to_dict()]).get()

        assert res1 is True
        assert res2 is False  # duplicate message skipped by consumer deduplication
        dispute.refresh_from_db()
        assert dispute.status == Dispute.Status.ANALYST_REVIEW
        assert DisputeRiskReport.objects.filter(dispute=dispute).count() == 1
        assert Transfer.objects.filter(agreement=agreement).count() == 0
