from __future__ import annotations

import json
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.pii import CustomerIdentity, envelope_cipher
from escrow.agreements.services import checkout_token_hash, customer_pii_context
from escrow.integrations.key_service import create_api_key
from escrow.integrations.rate_limit import RateLimitDecision
from escrow.ledger.models import LedgerEntry, LedgerTransaction
from escrow.ledger.services import LedgerEntryInput, LedgerPosting, post_ledger_transaction
from escrow.ledger.tasks import release_funds
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.models import OutboxEvent
from escrow.organizations.models import Organization, OrganizationMember
from escrow.payments.models import Transfer


class DeliveryToReleaseEndToEndTests(TestCase):
    """Drive the public seams from delivery report to the released BRL balance."""

    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="E2E release organization")
        user = get_user_model().objects.create_user(
            email="finance@e2e-release.test",
            password="Uma senha forte e exclusiva 2026!",
        )
        OrganizationMember.objects.create(
            organization=self.organization,
            user=user,
            role=OrganizationMember.Role.FINANCE,
        )
        self.client.force_login(user)
        _, self.api_key = create_api_key(
            self.organization,
            name="E2E delivery reporter",
            scopes=["agreements:write"],
        )
        self.checkout_token = "chk_e2e-release-capability"
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
            external_customer_id="buyer-e2e-release-001",
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
            status=EscrowAgreement.Status.HELD,
        )
        funding = Transfer.objects.create(
            agreement=self.agreement,
            kind=Transfer.Kind.FUNDING,
            status=Transfer.Status.COMPLETED,
            amount_minor=50_000,
            currency="BRL",
            provider=Transfer.Provider.SANDBOX_PIX,
            provider_reference="e2e-release-funding",
            idempotency_key="e2e-release-funding",
        )
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=funding.id,
                kind=LedgerTransaction.Kind.FUNDS_HELD,
                currency="BRL",
                idempotency_key="e2e-release-held",
                entries=(
                    LedgerEntryInput.debit("FUNDS_PENDING_RISK", 50_000, "BRL"),
                    LedgerEntryInput.credit("ESCROW_LIABILITY", 50_000, "BRL"),
                ),
            )
        )

    @patch(
        "escrow.integrations.authentication.check_api_key_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch(
        "escrow.delivery.views.check_customer_otp_verify_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch(
        "escrow.delivery.views.check_customer_otp_send_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch("escrow.delivery.services.send_customer_acceptance_otp")
    @patch("escrow.delivery.services._new_otp_code", return_value="123456")
    def test_delivery_to_released_balance_through_the_public_seams(
        self,
        _: object,
        send_email: object,
        __: object,
        ___: object,
        ____: object,
    ) -> None:
        reported = self.client.post(
            f"/api/v1/agreements/{self.agreement.id}/delivery/",
            data=json.dumps({}),
            content_type="application/json",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Idempotency-Key": "e2e-delivery-report-001",
            },
        )
        assert reported.status_code == 202
        assert reported.json()["status"] == "INSPECTION"

        otp_path = f"/api/v1/checkout/{self.checkout_token}/delivery-acceptance/otp/"
        requested = self.client.post(otp_path, data=json.dumps({}), content_type="application/json")
        challenge_id = requested.json()["challenge_id"]
        send_email.assert_called_once_with("bia@example.test", "123456")  # type: ignore[attr-defined]
        verified = self.client.post(
            f"{otp_path}{challenge_id}/verify/",
            data=json.dumps({"code": "123456"}),
            content_type="application/json",
        )
        accepted = self.client.post(
            f"/api/v1/checkout/{self.checkout_token}/delivery-acceptance/",
            data=json.dumps(
                {
                    "challenge_id": challenge_id,
                    "acceptance_token": verified.json()["acceptance_token"],
                }
            ),
            content_type="application/json",
        )
        assert accepted.status_code == 202
        assert accepted.json()["status"] == "PROCESSING"

        outbox_event = OutboxEvent.objects.get(message_type="ReleaseFunds.v1")
        envelope = MessageEnvelope.build(
            message_id=outbox_event.id,
            message_type=outbox_event.message_type,
            version=outbox_event.version,
            occurred_at=outbox_event.occurred_at,
            correlation_id=outbox_event.correlation_id,
            causation_id=outbox_event.causation_id,
            tenant_id=outbox_event.tenant_id,
            payload=outbox_event.payload,
        )
        release_funds.apply(args=[envelope.to_dict()]).get()

        self.agreement.refresh_from_db()
        assert self.agreement.status == EscrowAgreement.Status.RELEASED

        released = LedgerTransaction.objects.get(kind=LedgerTransaction.Kind.FUNDS_RELEASED)
        entries = LedgerEntry.objects.filter(ledger_transaction=released)
        assert set(entries.values_list("account__code", "debit_minor", "credit_minor")) == {
            ("ESCROW_LIABILITY", 50_000, 0),
            ("ORGANIZATION_PAYABLE", 0, 49_000),
            ("PLATFORM_FEE_REVENUE", 0, 1_000),
        }

        dashboard = self.client.get("/api/v1/organizations/current/")
        assert dashboard.status_code == 200
        assert dashboard.json()["balances"] == {
            "held_brl_minor": 0,
            "available_brl_minor": 49_000,
        }
        assert dashboard.json()["upcoming_releases"] == []

        allowed = RateLimitDecision(allowed=True, retry_after_seconds=0)
        with patch(
            "escrow.agreements.views.check_public_checkout_rate_limit",
            return_value=allowed,
        ):
            checkout = self.client.get(f"/api/v1/checkout/{self.checkout_token}/")
        assert checkout.status_code == 200
        assert checkout.json()["agreement"]["status"] == "RELEASED"
