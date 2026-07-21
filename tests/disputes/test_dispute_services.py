from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.pii import CustomerIdentity, envelope_cipher
from escrow.agreements.services import checkout_token_hash, customer_pii_context
from escrow.audit.models import AuditEvent
from escrow.delivery.services import (
    CustomerOtpStateConflict,
    accept_customer_delivery,
    enqueue_expired_inspection_releases,
    request_customer_acceptance_otp,
    verify_customer_acceptance_otp,
)
from escrow.disputes.models import Dispute
from escrow.disputes.services import (
    DisputeAlreadyOpen,
    DisputeStateConflict,
    open_dispute_after_customer_authorization,
)
from escrow.organizations.models import Organization
from escrow.payments.models import Transfer


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

    def test_a_second_dispute_for_the_same_agreement_is_rejected(self) -> None:
        open_dispute_after_customer_authorization(
            agreement_id=self.agreement.id,
            correlation_id="dispute-open-002",
        )

        with self.assertRaises(DisputeAlreadyOpen):
            open_dispute_after_customer_authorization(
                agreement_id=self.agreement.id,
                correlation_id="dispute-open-003",
            )

        assert Dispute.objects.filter(agreement=self.agreement).count() == 1

    def test_an_expired_inspection_can_no_longer_be_disputed(self) -> None:
        self.agreement.inspection_deadline_at = timezone.now() - timedelta(seconds=1)
        self.agreement.save(update_fields=["inspection_deadline_at"])

        with self.assertRaises(DisputeStateConflict):
            open_dispute_after_customer_authorization(
                agreement_id=self.agreement.id,
                correlation_id="dispute-open-004",
            )

        self.agreement.refresh_from_db()
        assert self.agreement.status == EscrowAgreement.Status.INSPECTION

    def test_a_disputed_agreement_is_never_released_by_the_inspection_scheduler(self) -> None:
        deadline = timezone.now() + timedelta(hours=1)
        self.agreement.inspection_deadline_at = deadline
        self.agreement.save(update_fields=["inspection_deadline_at"])
        open_dispute_after_customer_authorization(
            agreement_id=self.agreement.id,
            correlation_id="dispute-open-005",
        )

        enqueued = enqueue_expired_inspection_releases(now=deadline + timedelta(days=1))

        assert enqueued == 0
        self.agreement.refresh_from_db()
        assert self.agreement.status == EscrowAgreement.Status.DISPUTED
        assert Transfer.objects.filter(
            agreement=self.agreement,
            kind=Transfer.Kind.RELEASE,
        ).count() == 0


class DisputeAfterAcceptanceRaceTests(TestCase):
    def setUp(self) -> None:
        organization = Organization.objects.create(name="Loja corrida disputa")
        self.checkout_token = "chk_dispute-acceptance-race"
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
            external_customer_id="buyer-race-001",
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
            status=EscrowAgreement.Status.INSPECTION,
            inspection_deadline_at=timezone.now() + timedelta(days=1),
        )

    @patch("escrow.delivery.services.send_customer_acceptance_otp")
    @patch("escrow.delivery.services._new_otp_code", return_value="123456")
    def test_a_dispute_opened_after_customer_acceptance_conflicts(
        self,
        _: object,
        send_email: object,
    ) -> None:
        requested = request_customer_acceptance_otp(
            checkout_token=self.checkout_token,
            correlation_id="race-otp-001",
        )
        verified = verify_customer_acceptance_otp(
            checkout_token=self.checkout_token,
            challenge_id=requested.challenge.id,
            code="123456",
        )
        accept_customer_delivery(
            checkout_token=self.checkout_token,
            challenge_id=requested.challenge.id,
            acceptance_token=verified.acceptance_token,
            correlation_id="race-accept-001",
        )

        with self.assertRaises(DisputeStateConflict):
            open_dispute_after_customer_authorization(
                agreement_id=self.agreement.id,
                correlation_id="race-dispute-001",
            )

        self.agreement.refresh_from_db()
        assert self.agreement.status == EscrowAgreement.Status.RELEASE_PENDING
        assert Dispute.objects.filter(agreement=self.agreement).count() == 0

    @patch("escrow.delivery.services.send_customer_acceptance_otp")
    @patch("escrow.delivery.services._new_otp_code", return_value="123456")
    def test_a_customer_acceptance_after_a_dispute_conflicts(
        self,
        _: object,
        send_email: object,
    ) -> None:
        requested = request_customer_acceptance_otp(
            checkout_token=self.checkout_token,
            correlation_id="race-otp-002",
        )
        verified = verify_customer_acceptance_otp(
            checkout_token=self.checkout_token,
            challenge_id=requested.challenge.id,
            code="123456",
        )
        open_dispute_after_customer_authorization(
            agreement_id=self.agreement.id,
            correlation_id="race-dispute-002",
        )

        with self.assertRaises(CustomerOtpStateConflict):
            accept_customer_delivery(
                checkout_token=self.checkout_token,
                challenge_id=requested.challenge.id,
                acceptance_token=verified.acceptance_token,
                correlation_id="race-accept-002",
            )

        self.agreement.refresh_from_db()
        assert self.agreement.status == EscrowAgreement.Status.DISPUTED
        assert Transfer.objects.filter(
            agreement=self.agreement,
            kind=Transfer.Kind.RELEASE,
        ).count() == 0
