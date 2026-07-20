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
        self._post_release(released)

        response = self.client.get("/api/v1/organizations/current/")

        assert response.status_code == 200
        assert response.json()["balances"] == {
            "held_brl_minor": 50_000,
            "held_usd_minor": 0,
            "available_brl_minor": 49_000,
            "available_usd_minor": 0,
            "fee_brl_minor": 1_000,
            "fee_usd_minor": 0,
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

    def test_dashboard_keeps_brl_and_usd_balances_and_schedules_isolated(self) -> None:
        usd_scheduled = self._agreement(
            status=EscrowAgreement.Status.INSPECTION, currency="USD", amount_minor=20_000
        )
        usd_scheduled.inspection_deadline_at = timezone.now() + timedelta(days=3)
        usd_scheduled.save(update_fields=["inspection_deadline_at"])
        self._post_held(usd_scheduled, "usd-scheduled")

        brl_scheduled = self._agreement(status=EscrowAgreement.Status.INSPECTION)
        brl_scheduled.inspection_deadline_at = timezone.now() + timedelta(days=7)
        brl_scheduled.save(update_fields=["inspection_deadline_at"])
        self._post_held(brl_scheduled, "brl-scheduled")

        brl_released = self._agreement(status=EscrowAgreement.Status.RELEASED)
        self._post_held(brl_released, "brl-released")
        self._post_release(brl_released)

        response = self.client.get("/api/v1/organizations/current/")

        assert response.status_code == 200
        assert response.json()["balances"] == {
            "held_brl_minor": 50_000,
            "held_usd_minor": 20_000,
            "available_brl_minor": 49_000,
            "available_usd_minor": 0,
            "fee_brl_minor": 1_000,
            "fee_usd_minor": 0,
        }
        assert response.json()["upcoming_releases"] == [
            {
                "id": str(usd_scheduled.id),
                "currency": "USD",
                "gross_minor": 20_000,
                "fee_minor": 400,
                "net_minor": 19_600,
                "release_at": usd_scheduled.inspection_deadline_at.isoformat().replace(
                    "+00:00", "Z"
                ),
            },
            {
                "id": str(brl_scheduled.id),
                "currency": "BRL",
                "gross_minor": 50_000,
                "fee_minor": 1_000,
                "net_minor": 49_000,
                "release_at": brl_scheduled.inspection_deadline_at.isoformat().replace(
                    "+00:00", "Z"
                ),
            },
        ]

    def test_dashboard_serves_simulated_display_rates_without_financial_effects(self) -> None:
        ledger_before = list(
            LedgerTransaction.objects.order_by("created_at", "id").values(
                "kind", "currency", "posting_hash"
            )
        )

        first = self.client.get("/api/v1/organizations/current/")
        second = self.client.get("/api/v1/organizations/current/")

        assert first.status_code == 200
        assert first.json()["exchange_rates"] == [
            {
                "base_currency": "BRL",
                "quote_currency": "USD",
                "rate_micros": 180_000,
                "recorded_at": "2026-01-01T00:00:00Z",
                "is_simulated": True,
            },
            {
                "base_currency": "USD",
                "quote_currency": "BRL",
                "rate_micros": 5_400_000,
                "recorded_at": "2026-01-01T00:00:00Z",
                "is_simulated": True,
            },
        ]
        assert second.json()["exchange_rates"] == first.json()["exchange_rates"]
        ledger_after = list(
            LedgerTransaction.objects.order_by("created_at", "id").values(
                "kind", "currency", "posting_hash"
            )
        )
        assert ledger_after == ledger_before

    def _agreement(
        self, *, status: str, currency: str = "BRL", amount_minor: int = 50_000
    ) -> EscrowAgreement:
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
            amount_minor=amount_minor,
            currency=currency,
            fee_bps=200,
            delivery_window_days=7,
            status=status,
        )

    def _post_held(self, agreement: EscrowAgreement, suffix: str) -> None:
        transfer = Transfer.objects.create(
            agreement=agreement,
            kind=Transfer.Kind.FUNDING,
            status=Transfer.Status.COMPLETED,
            amount_minor=agreement.amount_minor,
            currency=agreement.currency,
            provider=Transfer.Provider.SANDBOX_PIX,
            provider_reference=f"dashboard-funding-{suffix}",
            idempotency_key=f"dashboard-funding-{suffix}",
        )
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=transfer.id,
                kind=LedgerTransaction.Kind.FUNDS_HELD,
                currency=agreement.currency,
                idempotency_key=f"dashboard-held-{suffix}",
                entries=(
                    LedgerEntryInput.debit(
                        "FUNDS_PENDING_RISK", agreement.amount_minor, agreement.currency
                    ),
                    LedgerEntryInput.credit(
                        "ESCROW_LIABILITY", agreement.amount_minor, agreement.currency
                    ),
                ),
            )
        )

    def _post_release(self, agreement: EscrowAgreement) -> None:
        release = Transfer.objects.create(
            agreement=agreement,
            kind=Transfer.Kind.RELEASE,
            status=Transfer.Status.COMPLETED,
            amount_minor=agreement.amount_minor,
            currency=agreement.currency,
            provider=Transfer.Provider.INTERNAL,
            provider_reference=f"release-{agreement.id.hex}",
            idempotency_key=f"release-{agreement.id}",
        )
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=release.id,
                kind=LedgerTransaction.Kind.FUNDS_RELEASED,
                currency=agreement.currency,
                idempotency_key=f"funds-released-{agreement.id}",
                entries=(
                    LedgerEntryInput.debit(
                        "ESCROW_LIABILITY", agreement.amount_minor, agreement.currency
                    ),
                    LedgerEntryInput.credit(
                        "ORGANIZATION_PAYABLE", agreement.amount_minor - 1_000, agreement.currency
                    ),
                    LedgerEntryInput.credit("PLATFORM_FEE_REVENUE", 1_000, agreement.currency),
                ),
            )
        )
