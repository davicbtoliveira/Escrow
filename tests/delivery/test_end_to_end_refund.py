from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.pii import CustomerIdentity, envelope_cipher
from escrow.agreements.services import checkout_token_hash, customer_pii_context
from escrow.audit.models import AuditEvent
from escrow.delivery.tasks import enqueue_expired_delivery_refunds_task
from escrow.integrations.models import WebhookEvent
from escrow.integrations.rate_limit import RateLimitDecision
from escrow.ledger.models import LedgerEntry, LedgerTransaction
from escrow.ledger.services import LedgerEntryInput, LedgerPosting, post_ledger_transaction
from escrow.ledger.tasks import refund_funds
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.models import OutboxEvent
from escrow.organizations.models import Organization, OrganizationMember
from escrow.payments.models import Transfer


class ExpiredDeliveryRefundEndToEndTests(TestCase):
    """Drive the public seams from an expired deadline to the refunded balances."""

    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="E2E refund organization")
        user = get_user_model().objects.create_user(
            email="finance@e2e-refund.test",
            password="Uma senha forte e exclusiva 2026!",
        )
        OrganizationMember.objects.create(
            organization=self.organization,
            user=user,
            role=OrganizationMember.Role.FINANCE,
        )
        self.client.force_login(user)
        self.checkout_token = "chk_e2e-refund-capability"
        agreement_id = uuid4()
        customer = CustomerIdentity(
            name="Bia Compradora",
            email="bia@example.test",
            document="52998224725",
            document_kind="CPF",
        )
        encrypted = envelope_cipher().encrypt(
            customer.plaintext(),
            customer_pii_context(self.organization.id, agreement_id),
        )
        self.agreement = EscrowAgreement.objects.create(
            id=agreement_id,
            organization=self.organization,
            external_customer_id="buyer-e2e-refund-001",
            customer_name_masked="Bia C.",
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
            funding_confirmed_at=timezone.now() - timedelta(days=8),
            delivery_due_at=timezone.now() - timedelta(seconds=1),
            status=EscrowAgreement.Status.HELD,
            realtime_sequence=2,
        )
        funding = Transfer.objects.create(
            agreement=self.agreement,
            kind=Transfer.Kind.FUNDING,
            status=Transfer.Status.COMPLETED,
            amount_minor=50_000,
            currency="BRL",
            provider=Transfer.Provider.SANDBOX_PIX,
            provider_reference="e2e-refund-funding",
            idempotency_key="e2e-refund-funding",
        )
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=funding.id,
                kind=LedgerTransaction.Kind.FUNDS_HELD,
                currency="BRL",
                idempotency_key="e2e-refund-held",
                entries=(
                    LedgerEntryInput.debit("FUNDS_PENDING_RISK", 50_000, "BRL"),
                    LedgerEntryInput.credit("ESCROW_LIABILITY", 50_000, "BRL"),
                ),
            )
        )

    def test_expired_deadline_refunds_through_every_surface(self) -> None:
        assert enqueue_expired_delivery_refunds_task.apply().get() == 1

        self.agreement.refresh_from_db()
        assert self.agreement.status == EscrowAgreement.Status.REFUND_PENDING

        command = OutboxEvent.objects.get(message_type="RefundFunds.v1")
        envelope = MessageEnvelope.build(
            message_id=command.id,
            message_type=command.message_type,
            version=command.version,
            occurred_at=command.occurred_at,
            correlation_id=command.correlation_id,
            causation_id=command.causation_id,
            tenant_id=command.tenant_id,
            payload=command.payload,
        )
        refund_funds.apply(args=[envelope.to_dict()]).get()

        self.agreement.refresh_from_db()
        assert self.agreement.status == EscrowAgreement.Status.REFUNDED

        refunded = LedgerTransaction.objects.get(kind=LedgerTransaction.Kind.FUNDS_REFUNDED)
        entries = LedgerEntry.objects.filter(ledger_transaction=refunded)
        assert set(entries.values_list("account__code", "debit_minor", "credit_minor")) == {
            ("ESCROW_LIABILITY", 50_000, 0),
            ("PIX_CLEARING", 0, 50_000),
        }

        dashboard = self.client.get("/api/v1/organizations/current/")
        assert dashboard.status_code == 200
        assert dashboard.json()["balances"] == {
            "held_brl_minor": 0,
            "held_usd_minor": 0,
            "available_brl_minor": 0,
            "available_usd_minor": 0,
            "fee_brl_minor": 0,
            "fee_usd_minor": 0,
        }
        assert dashboard.json()["upcoming_releases"] == []

        allowed = RateLimitDecision(allowed=True, retry_after_seconds=0)
        with patch(
            "escrow.agreements.views.check_public_checkout_rate_limit",
            return_value=allowed,
        ):
            checkout = self.client.get(f"/api/v1/checkout/{self.checkout_token}/")
        assert checkout.status_code == 200
        assert checkout.json()["agreement"]["status"] == "REFUNDED"
        assert checkout.json()["agreement"]["refund_reason"] == "DELIVERY_DEADLINE_EXPIRED"

        refunded_webhook = WebhookEvent.objects.get(
            agreement=self.agreement,
            payload__status="REFUNDED",
        )
        assert refunded_webhook.payload["refund_reason"] == "DELIVERY_DEADLINE_EXPIRED"
        assert (
            OutboxEvent.objects.filter(
                message_type="AgreementStatusChanged.v1",
                payload__status="REFUNDED",
            ).count()
            == 1
        )
        assert AuditEvent.objects.filter(
            agreement=self.agreement,
            event_type="funds_refunded",
        ).exists()
