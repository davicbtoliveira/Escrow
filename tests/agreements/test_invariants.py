from __future__ import annotations

import hashlib
import hmac
import json

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.services import (
    AgreementValidationError,
    InactiveOrganizationError,
    canonical_payload_hash,
    create_agreement,
    parse_agreement_input,
)
from escrow.organizations.models import Organization


@override_settings(PII_ENCRYPTION_BACKEND="local")
class AgreementDatabaseInvariantTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Loja Horizonte")
        self.command = parse_agreement_input(
            {
                "external_customer_id": "buyer-123",
                "customer": {
                    "name": "Ana da Silva",
                    "email": "ana.silva@example.test",
                    "document": "529.982.247-25",
                },
                "amount": "50000.00",
                "currency": "BRL",
                "delivery_window_days": 7,
            }
        )

    def test_locked_service_rechecks_a_deactivated_organization(self) -> None:
        self.organization.is_active = False
        self.organization.save(update_fields=["is_active"])

        with self.assertRaises(InactiveOrganizationError):
            create_agreement(
                organization_id=self.organization.id,
                command=self.command,
                payload_hash=canonical_payload_hash(self.command),
                idempotency_key="inactive-organization-001",
            )

        assert EscrowAgreement.objects.count() == 0

    def test_database_rejects_a_currency_outside_the_mvp_pair(self) -> None:
        result = create_agreement(
            organization_id=self.organization.id,
            command=self.command,
            payload_hash=canonical_payload_hash(self.command),
            idempotency_key="currency-constraint-001",
        )
        assert result.status == 201
        agreement = EscrowAgreement.objects.get()

        with self.assertRaises(IntegrityError), transaction.atomic():
            agreement.currency = "EUR"
            agreement.save(update_fields=["currency"])

    def test_parser_rejects_a_boolean_delivery_window(self) -> None:
        payload = {
            "external_customer_id": "buyer-123",
            "customer": {
                "name": "Ana da Silva",
                "email": "ana.silva@example.test",
                "document": "529.982.247-25",
            },
            "amount": "50000.00",
            "currency": "BRL",
            "delivery_window_days": True,
        }

        with self.assertRaises(AgreementValidationError):
            parse_agreement_input(payload)

    @override_settings(AGREEMENT_IDEMPOTENCY_HMAC_SECRET="test-idempotency-hmac-key")
    def test_canonical_idempotency_value_is_a_keyed_hmac_of_normalized_terms(self) -> None:
        expected_payload = {
            "external_customer_id": "buyer-123",
            "customer": {
                "name": "Ana da Silva",
                "email": "ana.silva@example.test",
                "document": "52998224725",
            },
            "amount": "50000.00",
            "currency": "BRL",
            "delivery_window_days": 7,
        }
        expected = hmac.new(
            b"test-idempotency-hmac-key",
            json.dumps(
                expected_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode(),
            hashlib.sha256,
        ).hexdigest()

        assert canonical_payload_hash(self.command) == expected
