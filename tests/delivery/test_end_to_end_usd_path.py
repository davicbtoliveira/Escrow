from __future__ import annotations

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.integrations.key_service import create_api_key
from escrow.integrations.rate_limit import RateLimitDecision
from escrow.ledger.models import LedgerEntry, LedgerTransaction
from escrow.ledger.tasks import post_funding, release_funds
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.models import OutboxEvent
from escrow.organizations.models import Organization, OrganizationMember
from escrow.payments.callbacks import sign_sandbox_callback
from escrow.payments.funding import process_sandbox_pix_callback
from escrow.payments.services import create_sandbox_pix_charge
from escrow.risk.models import FundingRiskDecision
from escrow.risk.tasks import evaluate_funding_risk

_CALLBACK_SECRET = "sandbox-pix-test-signing-secret"


def _envelope(event: OutboxEvent) -> dict[str, object]:
    return MessageEnvelope.build(
        message_id=event.id,
        message_type=event.message_type,
        version=event.version,
        occurred_at=event.occurred_at,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        tenant_id=event.tenant_id,
        payload=event.payload,
    ).to_dict()


@override_settings(PII_ENCRYPTION_BACKEND="local")
class UsdAgreementEndToEndTests(TestCase):
    """Drive one USD agreement through payment, risk, custody, delivery, and release."""

    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="USD path organization")
        user = get_user_model().objects.create_user(
            email="finance@usd-path.test",
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
            name="USD marketplace",
            scopes=["agreements:write"],
        )

    @patch(
        "escrow.agreements.views.check_public_checkout_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
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
    def test_usd_agreement_completes_the_full_custody_path_in_usd(
        self,
        _: object,
        send_email: object,
        __: object,
        ___: object,
        ____: object,
        _____: object,
    ) -> None:
        created = self.client.post(
            "/api/v1/agreements/",
            data=json.dumps(
                {
                    "external_customer_id": "buyer-usd-001",
                    "customer": {
                        "name": "Bia Compradora",
                        "email": "bia@example.test",
                        "document": "529.982.247-25",
                    },
                    "amount": "7500.00",
                    "currency": "USD",
                    "delivery_window_days": 7,
                }
            ),
            content_type="application/json",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Idempotency-Key": "usd-path-create-001",
            },
        )
        assert created.status_code == 201
        assert created.json()["agreement"]["amount"] == "7500.00"
        assert created.json()["agreement"]["currency"] == "USD"
        agreement_id = created.json()["agreement"]["id"]
        checkout_token = created.json()["checkout_url"].rsplit("/", 1)[-1]

        charge = create_sandbox_pix_charge(
            agreement_id=agreement_id,
            idempotency_key="usd-path-pix-charge-001",
        ).charge
        now = timezone.now()
        raw_body = json.dumps(
            {
                "event_id": "usd-path-callback-001",
                "provider_reference": charge.provider_reference,
                "outcome": "CONFIRMED",
            },
            separators=(",", ":"),
        ).encode()
        timestamp = str(int(now.timestamp()))
        callback = process_sandbox_pix_callback(
            raw_body=raw_body,
            timestamp=timestamp,
            signature=sign_sandbox_callback(_CALLBACK_SECRET, timestamp, raw_body),
            signing_secret=_CALLBACK_SECRET,
            correlation_id="usd-path-correlation-001",
            now=now,
        )
        assert callback.transfer is not None
        assert callback.transfer.currency == "USD"
        assert callback.transfer.amount_minor == 750_000

        risk_event = OutboxEvent.objects.get(message_type="EvaluateFundingRisk.v1")
        evaluate_funding_risk.apply(args=[_envelope(risk_event)]).get()
        decision = FundingRiskDecision.objects.get(transfer=callback.transfer)
        assert decision.outcome == "APPROVED"

        ledger_event = OutboxEvent.objects.get(message_type="PostFunding.v1")
        post_funding.apply(args=[_envelope(ledger_event)]).get()

        agreement = EscrowAgreement.objects.get(id=agreement_id)
        assert agreement.status == EscrowAgreement.Status.HELD

        reported = self.client.post(
            f"/api/v1/agreements/{agreement_id}/delivery/",
            data=json.dumps({}),
            content_type="application/json",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Idempotency-Key": "usd-path-delivery-001",
            },
        )
        assert reported.status_code == 202
        assert reported.json()["status"] == "INSPECTION"

        otp_path = f"/api/v1/checkout/{checkout_token}/delivery-acceptance/otp/"
        requested = self.client.post(otp_path, data=json.dumps({}), content_type="application/json")
        challenge_id = requested.json()["challenge_id"]
        send_email.assert_called_once_with("bia@example.test", "123456")  # type: ignore[attr-defined]
        verified = self.client.post(
            f"{otp_path}{challenge_id}/verify/",
            data=json.dumps({"code": "123456"}),
            content_type="application/json",
        )
        accepted = self.client.post(
            f"/api/v1/checkout/{checkout_token}/delivery-acceptance/",
            data=json.dumps(
                {
                    "challenge_id": challenge_id,
                    "acceptance_token": verified.json()["acceptance_token"],
                }
            ),
            content_type="application/json",
        )
        assert accepted.status_code == 202

        release_event = OutboxEvent.objects.get(message_type="ReleaseFunds.v1")
        release_funds.apply(args=[_envelope(release_event)]).get()

        agreement.refresh_from_db()
        assert agreement.status == EscrowAgreement.Status.RELEASED

        posted_currencies = set(LedgerTransaction.objects.values_list("currency", flat=True)) | set(
            LedgerEntry.objects.values_list("currency", flat=True)
        )
        assert posted_currencies == {"USD"}

        released = LedgerTransaction.objects.get(kind=LedgerTransaction.Kind.FUNDS_RELEASED)
        entries = LedgerEntry.objects.filter(ledger_transaction=released)
        assert set(
            entries.values_list("account__code", "debit_minor", "credit_minor", "currency")
        ) == {
            ("ESCROW_LIABILITY", 750_000, 0, "USD"),
            ("ORGANIZATION_PAYABLE", 0, 735_000, "USD"),
            ("PLATFORM_FEE_REVENUE", 0, 15_000, "USD"),
        }
        debit_total = sum(entry.debit_minor for entry in entries)
        credit_total = sum(entry.credit_minor for entry in entries)
        assert debit_total == credit_total == 750_000

        dashboard = self.client.get("/api/v1/organizations/current/")
        assert dashboard.status_code == 200
        assert dashboard.json()["balances"] == {
            "held_brl_minor": 0,
            "held_usd_minor": 0,
            "available_brl_minor": 0,
            "available_usd_minor": 735_000,
            "fee_brl_minor": 0,
            "fee_usd_minor": 15_000,
        }

        checkout = self.client.get(f"/api/v1/checkout/{checkout_token}/")
        assert checkout.status_code == 200
        assert checkout.json()["agreement"]["status"] == "RELEASED"
        assert checkout.json()["agreement"]["amount"] == "7500.00"
        assert checkout.json()["agreement"]["currency"] == "USD"
