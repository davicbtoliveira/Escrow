from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.ledger.models import LedgerTransaction
from escrow.ledger.services import LedgerEntryInput, LedgerPosting, post_ledger_transaction
from escrow.organizations.models import Organization, OrganizationMember
from escrow.payments.models import Transfer


class OrganizationFinanceDashboardTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Finance dashboard")
        user = get_user_model().objects.create_user(
            email="finance@dashboard.test",
            password="Uma senha forte e exclusiva 2026!",
        )
        OrganizationMember.objects.create(
            organization=self.organization,
            user=user,
            role=OrganizationMember.Role.FINANCE,
        )
        self.client.force_login(user)

    def test_dashboard_separates_held_scheduled_gross_fee_net_and_available_value(self) -> None:
        scheduled = self._agreement(status=EscrowAgreement.Status.INSPECTION)
        scheduled.inspection_deadline_at = timezone.now() + timedelta(days=7)
        scheduled.save(update_fields=["inspection_deadline_at"])
        self._post_held(scheduled, "scheduled")

        released = self._agreement(status=EscrowAgreement.Status.RELEASED)
        self._post_held(released, "released")
        release = Transfer.objects.create(
            agreement=released,
            kind=Transfer.Kind.RELEASE,
            status=Transfer.Status.COMPLETED,
            amount_minor=50_000,
            currency="BRL",
            provider=Transfer.Provider.INTERNAL,
            provider_reference=f"release-{released.id.hex}",
            idempotency_key=f"release-{released.id}",
        )
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=release.id,
                kind=LedgerTransaction.Kind.FUNDS_RELEASED,
                currency="BRL",
                idempotency_key=f"funds-released-{released.id}",
                entries=(
                    LedgerEntryInput.debit("ESCROW_LIABILITY", 50_000, "BRL"),
                    LedgerEntryInput.credit("ORGANIZATION_PAYABLE", 49_000, "BRL"),
                    LedgerEntryInput.credit("PLATFORM_FEE_REVENUE", 1_000, "BRL"),
                ),
            )
        )

        response = self.client.get("/api/v1/organizations/current/")

        assert response.status_code == 200
        assert response.json()["balances"] == {
            "held_brl_minor": 50_000,
            "available_brl_minor": 49_000,
        }
        assert response.json()["upcoming_releases"] == [
            {
                "id": str(scheduled.id),
                "currency": "BRL",
                "gross_minor": 50_000,
                "fee_minor": 1_000,
                "net_minor": 49_000,
                "release_at": scheduled.inspection_deadline_at.isoformat().replace("+00:00", "Z"),
            }
        ]

    def _agreement(self, *, status: str) -> EscrowAgreement:
        return EscrowAgreement.objects.create(
            organization=self.organization,
            external_customer_id=f"dashboard-{uuid4().hex}",
            customer_name_masked="A***",
            customer_email_masked="a***@example.test",
            customer_document_masked="***.***.***-25",
            customer_document_kind="CPF",
            customer_email_blind_index=uuid4().hex * 2,
            customer_document_blind_index=uuid4().hex * 2,
            customer_pii_ciphertext=b"ciphertext",
            customer_pii_nonce=b"nonce",
            customer_pii_encrypted_data_key=b"encrypted-key",
            customer_pii_kms_key_id="test-key",
            checkout_token_hash=uuid4().hex * 2,
            amount_minor=50_000,
            currency="BRL",
            fee_bps=200,
            delivery_window_days=7,
            status=status,
        )

    def _post_held(self, agreement: EscrowAgreement, suffix: str) -> None:
        transfer = Transfer.objects.create(
            agreement=agreement,
            kind=Transfer.Kind.FUNDING,
            status=Transfer.Status.COMPLETED,
            amount_minor=50_000,
            currency="BRL",
            provider=Transfer.Provider.SANDBOX_PIX,
            provider_reference=f"dashboard-funding-{suffix}",
            idempotency_key=f"dashboard-funding-{suffix}",
        )
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=transfer.id,
                kind=LedgerTransaction.Kind.FUNDS_HELD,
                currency="BRL",
                idempotency_key=f"dashboard-held-{suffix}",
                entries=(
                    LedgerEntryInput.debit("FUNDS_PENDING_RISK", 50_000, "BRL"),
                    LedgerEntryInput.credit("ESCROW_LIABILITY", 50_000, "BRL"),
                ),
            )
        )

