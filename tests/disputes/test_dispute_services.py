from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.audit.models import AuditEvent
from escrow.disputes.models import Dispute
from escrow.disputes.services import open_dispute_after_customer_authorization
from escrow.organizations.models import Organization


class OpenDisputeTests(TestCase):
    def setUp(self) -> None:
        organization = Organization.objects.create(name="Loja de disputa")
        self.agreement = EscrowAgreement.objects.create(
            organization=organization,
            external_customer_id=f"customer-{uuid4().hex}",
            customer_name_masked="A*** C****",
            customer_email_masked="a***@example.test",
            customer_document_masked="***.***.***-25",
            customer_document_kind="CPF",
            customer_email_blind_index=uuid4().hex * 2,
            customer_document_blind_index=uuid4().hex * 2,
            customer_pii_ciphertext=b"ciphertext",
            customer_pii_nonce=b"nonce",
            customer_pii_encrypted_data_key=b"encrypted-key",
            customer_pii_kms_key_id="local-test-key",
            checkout_token_hash=uuid4().hex * 2,
            amount_minor=50_000,
            currency="BRL",
            fee_bps=200,
            delivery_window_days=7,
            status=EscrowAgreement.Status.INSPECTION,
            inspection_deadline_at=timezone.now() + timedelta(days=1),
        )

    def test_customer_authorized_dispute_freezes_an_open_inspection_once(self) -> None:
        result = open_dispute_after_customer_authorization(
            agreement_id=self.agreement.id,
            correlation_id="dispute-open-001",
        )

        self.agreement.refresh_from_db()
        assert result.dispute.status == Dispute.Status.OPEN
        assert result.dispute.agreement_id == self.agreement.id
        assert result.dispute.sla_due_at == result.dispute.opened_at + timedelta(hours=72)
        assert self.agreement.status == EscrowAgreement.Status.DISPUTED
        assert Dispute.objects.filter(agreement=self.agreement).count() == 1
        audit = AuditEvent.objects.get(event_type="dispute_opened")
        assert audit.agreement_id == self.agreement.id
        assert audit.payload == {"dispute_id": str(result.dispute.id)}
