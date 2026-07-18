from __future__ import annotations

import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from escrow.agreements.models import EscrowAgreement, IdempotencyRecord
from escrow.agreements.money import parse_minor_amount
from escrow.agreements.pii import (
    CustomerIdentityValidationError,
    EncryptedValue,
    envelope_cipher,
    validate_customer_identity,
)
from escrow.agreements.services import customer_pii_context
from escrow.integrations.key_service import create_api_key
from escrow.integrations.rate_limit import RateLimitDecision
from escrow.organizations.models import Organization


@override_settings(PII_ENCRYPTION_BACKEND="local")
class AgreementCreationApiTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Loja Horizonte")
        _, self.api_key = create_api_key(
            self.organization,
            name="Marketplace",
            scopes=["agreements:write"],
        )

    def payload(self) -> dict[str, object]:
        return {
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

    @patch(
        "escrow.integrations.authentication.check_api_key_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_authorized_organization_creates_an_awaiting_payment_agreement(
        self,
        _: object,
    ) -> None:
        response = self.client.post(
            "/api/v1/agreements/",
            data=json.dumps(self.payload()),
            content_type="application/json",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Idempotency-Key": "agreement-create-001",
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["agreement"]["status"] == "AWAITING_PAYMENT"
        assert body["agreement"]["amount"] == "50000.00"
        assert body["agreement"]["currency"] == "BRL"
        assert body["agreement"]["fee_bps"] == 200
        assert body["agreement"]["delivery_window_days"] == 7
        assert body["checkout_url"].startswith("http://localhost:5173/checkout/")

    @patch(
        "escrow.integrations.authentication.check_api_key_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_creation_requires_an_idempotency_key(
        self,
        _: object,
    ) -> None:
        response = self.client.post(
            "/api/v1/agreements/",
            data=json.dumps(self.payload()),
            content_type="application/json",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

        assert response.status_code == 400
        assert response.json()["code"] == "idempotency_key_required"

    @override_settings(AGREEMENT_IDEMPOTENCY_HMAC_SECRET="")
    @patch(
        "escrow.integrations.authentication.check_api_key_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_creation_fails_closed_when_its_idempotency_hmac_secret_is_unavailable(
        self,
        _: object,
    ) -> None:
        response = self.client.post(
            "/api/v1/agreements/",
            data=json.dumps(self.payload()),
            content_type="application/json",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Idempotency-Key": "agreement-missing-hmac-key-001",
            },
        )

        assert response.status_code == 503
        assert response.json()["code"] == "idempotency_unavailable"
        assert EscrowAgreement.objects.count() == 0

    @patch(
        "escrow.integrations.authentication.check_api_key_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_same_idempotency_key_replays_once_but_conflicting_payload_is_rejected(
        self,
        _: object,
    ) -> None:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Idempotency-Key": "agreement-create-retry-001",
        }
        first = self.client.post(
            "/api/v1/agreements/",
            data=json.dumps(self.payload()),
            content_type="application/json",
            headers=headers,
        )
        replay = self.client.post(
            "/api/v1/agreements/",
            data=json.dumps(self.payload()),
            content_type="application/json",
            headers=headers,
        )
        conflicting_payload = self.payload()
        conflicting_payload["amount"] = "50000.01"
        conflict = self.client.post(
            "/api/v1/agreements/",
            data=json.dumps(conflicting_payload),
            content_type="application/json",
            headers=headers,
        )

        assert first.status_code == 201
        assert replay.status_code == 201
        assert replay.json() == first.json()
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "idempotency_key_reused"
        assert EscrowAgreement.objects.count() == 1
        record = IdempotencyRecord.objects.get()
        checkout_token = first.json()["checkout_url"].rsplit("/", maxsplit=1)[-1]
        assert IdempotencyRecord.objects.count() == 1
        assert "checkout_url" not in record.response_body
        assert checkout_token not in json.dumps(record.response_body)
        assert checkout_token not in bytes(record.checkout_token_ciphertext).decode(errors="ignore")

    @patch(
        "escrow.integrations.authentication.check_api_key_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_idempotency_compares_normalized_customer_and_money_terms(
        self,
        _: object,
    ) -> None:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Idempotency-Key": "agreement-semantic-retry-001",
        }
        equivalent_payload = self.payload()
        equivalent_payload["customer"] = {
            "name": "  Ana da Silva  ",
            "email": "ANA.SILVA@EXAMPLE.TEST",
            "document": "52998224725",
        }
        equivalent_payload["amount"] = "50000"
        first = self.client.post(
            "/api/v1/agreements/",
            data=json.dumps(self.payload()),
            content_type="application/json",
            headers=headers,
        )
        replay = self.client.post(
            "/api/v1/agreements/",
            data=json.dumps(equivalent_payload),
            content_type="application/json",
            headers=headers,
        )

        assert first.status_code == 201
        assert replay.status_code == 201
        assert replay.json() == first.json()
        assert EscrowAgreement.objects.count() == 1

    @patch(
        "escrow.integrations.authentication.check_api_key_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_creation_persists_only_masked_or_encrypted_customer_identity_and_fee_snapshot(
        self,
        _: object,
    ) -> None:
        response = self.client.post(
            "/api/v1/agreements/",
            data=json.dumps(self.payload()),
            content_type="application/json",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Idempotency-Key": "agreement-pii-001",
            },
        )
        agreement = EscrowAgreement.objects.get()
        self.organization.fee_bps = 450
        self.organization.save(update_fields=["fee_bps"])
        agreement.refresh_from_db()
        encrypted = EncryptedValue(
            ciphertext=bytes(agreement.customer_pii_ciphertext),
            nonce=bytes(agreement.customer_pii_nonce),
            encrypted_data_key=bytes(agreement.customer_pii_encrypted_data_key),
            kms_key_id=agreement.customer_pii_kms_key_id,
        )
        decrypted = envelope_cipher().decrypt(
            encrypted,
            customer_pii_context(agreement.organization_id, agreement.id),
        )

        assert response.status_code == 201
        assert agreement.amount_minor == 5_000_000
        assert agreement.fee_bps == 200
        assert agreement.delivery_due_at is None
        assert agreement.inspection_deadline_at is None
        assert agreement.customer_name_masked == "Ana S."
        assert agreement.customer_email_masked == "a***@example.test"
        assert agreement.customer_document_masked == "***.***.***-25"
        assert agreement.customer_email_blind_index != "ana.silva@example.test"
        assert agreement.customer_document_blind_index != "52998224725"
        assert len(agreement.customer_document_blind_index) == 64
        assert b"ana.silva@example.test" not in bytes(agreement.customer_pii_ciphertext)
        assert b"52998224725" not in bytes(agreement.customer_pii_ciphertext)
        assert json.loads(decrypted) == {
            "name": "Ana da Silva",
            "email": "ana.silva@example.test",
            "document": "52998224725",
        }
        assert "ana.silva@example.test" not in response.content.decode()
        assert "52998224725" not in response.content.decode()

    @patch(
        "escrow.integrations.authentication.check_api_key_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_creation_rejects_ambiguous_money_currency_deadline_and_document(
        self,
        _: object,
    ) -> None:
        invalid_payloads = []
        numeric_amount = self.payload()
        numeric_amount["amount"] = 50_000.0
        invalid_payloads.append(numeric_amount)
        excessive_precision = self.payload()
        excessive_precision["amount"] = "50000.001"
        invalid_payloads.append(excessive_precision)
        bigint_overflow = self.payload()
        bigint_overflow["amount"] = "92233720368547758.08"
        invalid_payloads.append(bigint_overflow)
        unsupported_currency = self.payload()
        unsupported_currency["currency"] = "EUR"
        invalid_payloads.append(unsupported_currency)
        too_short_delivery = self.payload()
        too_short_delivery["delivery_window_days"] = 0
        invalid_payloads.append(too_short_delivery)
        too_long_delivery = self.payload()
        too_long_delivery["delivery_window_days"] = 91
        invalid_payloads.append(too_long_delivery)
        invalid_document = self.payload()
        invalid_document["customer"] = {
            **invalid_document["customer"],
            "document": "111.111.111-11",
        }
        invalid_payloads.append(invalid_document)

        for index, payload in enumerate(invalid_payloads):
            with self.subTest(index=index):
                response = self.client.post(
                    "/api/v1/agreements/",
                    data=json.dumps(payload),
                    content_type="application/json",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Idempotency-Key": f"invalid-agreement-{index}",
                    },
                )

                assert response.status_code == 400
                assert response.json()["code"] == "validation_error"
        assert EscrowAgreement.objects.count() == 0

    @patch(
        "escrow.agreements.views.check_public_checkout_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch(
        "escrow.integrations.authentication.check_api_key_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_public_checkout_returns_only_its_masked_whitelist_with_private_headers(
        self,
        _: object,
        __: object,
    ) -> None:
        creation = self.client.post(
            "/api/v1/agreements/",
            data=json.dumps(self.payload()),
            content_type="application/json",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Idempotency-Key": "agreement-checkout-001",
            },
        )
        checkout_token = creation.json()["checkout_url"].rsplit("/", maxsplit=1)[-1]
        checkout = self.client.get(f"/api/v1/checkout/{checkout_token}/")
        other_checkout = self.client.get(f"/api/v1/checkout/{checkout_token}x/")

        assert checkout.status_code == 200
        assert checkout["Cache-Control"] == "no-store, private"
        assert checkout["Referrer-Policy"] == "no-referrer"
        assert set(checkout.json()) == {"agreement"}
        agreement = checkout.json()["agreement"]
        assert set(agreement) == {
            "id",
            "status",
            "customer",
            "amount",
            "currency",
            "delivery_window_days",
            "delivery_due_at",
            "fee_bps",
        }
        assert agreement["customer"] == {
            "name": "Ana S.",
            "email_masked": "a***@example.test",
            "document_masked": "***.***.***-25",
        }
        assert "ana.silva@example.test" not in checkout.content.decode()
        assert "52998224725" not in checkout.content.decode()
        assert checkout_token not in checkout.content.decode()
        assert other_checkout.status_code == 404
        assert other_checkout["Cache-Control"] == "no-store, private"

    @patch(
        "escrow.agreements.views.check_public_checkout_rate_limit",
        return_value=RateLimitDecision(allowed=False, retry_after_seconds=17),
    )
    def test_public_checkout_is_rate_limited_without_echoing_its_capability(
        self,
        _: object,
    ) -> None:
        checkout_token = "chk_not-a-real-checkout-token"

        response = self.client.get(f"/api/v1/checkout/{checkout_token}/")

        assert response.status_code == 429
        assert response.json()["code"] == "public_checkout_rate_limited"
        assert response["Retry-After"] == "17"
        assert response["Cache-Control"] == "no-store, private"
        assert checkout_token not in response.content.decode()

    @override_settings(CHECKOUT_TOKEN_HMAC_SECRET="")
    @patch(
        "escrow.agreements.views.check_public_checkout_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    def test_public_checkout_fails_closed_when_its_token_key_is_unavailable(
        self,
        _: object,
    ) -> None:
        response = self.client.get("/api/v1/checkout/chk_unavailable-key/")

        assert response.status_code == 503
        assert response.json()["code"] == "pii_encryption_unavailable"
        assert response["Cache-Control"] == "no-store, private"


class DecimalMoneyTests(TestCase):
    def test_decimal_strings_convert_exactly_to_minor_units(self) -> None:
        assert parse_minor_amount("1", "BRL") == (100, "BRL")
        assert parse_minor_amount("1.2", "BRL") == (120, "BRL")
        assert parse_minor_amount("1.20", "USD") == (120, "USD")
        assert parse_minor_amount("50000.00", "BRL") == (5_000_000, "BRL")


class CustomerDocumentValidationTests(TestCase):
    def test_cpf_and_cnpj_are_normalized_but_non_document_characters_are_rejected(self) -> None:
        cpf = validate_customer_identity(
            {
                "name": "Ana da Silva",
                "email": "ana@example.test",
                "document": "529.982.247-25",
            }
        )
        cnpj = validate_customer_identity(
            {
                "name": "Loja Horizonte",
                "email": "financeiro@example.test",
                "document": "04.252.011/0001-10",
            }
        )

        assert (cpf.document, cpf.document_kind) == ("52998224725", "CPF")
        assert (cnpj.document, cnpj.document_kind) == ("04252011000110", "CNPJ")
        with self.assertRaises(CustomerIdentityValidationError):
            validate_customer_identity(
                {
                    "name": "Ana da Silva",
                    "email": "ana@example.test",
                    "document": "abc529.982.247-25",
                }
            )
