from __future__ import annotations

from django.test import SimpleTestCase


class AgreementOpenApiTests(SimpleTestCase):
    def test_schema_describes_idempotent_creation_and_a_strict_public_checkout_shape(self) -> None:
        response = self.client.get("/api/v1/openapi.json")

        assert response.status_code == 200
        schema = response.json()
        create_operation = schema["paths"]["/api/v1/agreements/"]["post"]
        public_operation = schema["paths"]["/api/v1/checkout/{checkout_token}/"]["get"]
        public_agreement = schema["components"]["schemas"]["PublicAgreement"]

        assert create_operation["security"] == [{"ApiKeyAuth": []}]
        assert {parameter["name"] for parameter in create_operation["parameters"]} == {
            "Authorization",
            "Idempotency-Key",
        }
        assert "security" not in public_operation
        assert "external_customer_id" not in public_agreement["properties"]
