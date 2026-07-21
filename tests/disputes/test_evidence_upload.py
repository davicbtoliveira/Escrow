from __future__ import annotations

import hashlib
import json
from io import BytesIO
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.pii import CustomerIdentity, envelope_cipher
from escrow.agreements.services import checkout_token_hash, customer_pii_context
from escrow.audit.models import AuditEvent
from escrow.delivery.services import report_delivery
from escrow.disputes.models import Evidence
from escrow.integrations.rate_limit import RateLimitDecision
from escrow.organizations.models import Organization


class FakeEvidenceS3Client:
    """In-memory double for the Ceph RGW boundary used by upload tests."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:
        self.objects[(Bucket, Key)] = (Body, ContentType)


class CustomerEvidenceUploadApiTests(TestCase):
    def setUp(self) -> None:
        organization = Organization.objects.create(name="Loja evidencia upload")
        self.checkout_token = "chk_customer-evidence-upload"
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
            external_customer_id="buyer-evidence-001",
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
            status=EscrowAgreement.Status.HELD,
        )
        report_delivery(
            organization_id=organization.id,
            agreement_id=self.agreement.id,
            idempotency_key="delivery-report-evidence-001",
            correlation_id="customer-evidence-test",
        )
        self.storage = FakeEvidenceS3Client()

    def _open_dispute(self) -> tuple[str, str, str]:
        otp_path = f"/api/v1/checkout/{self.checkout_token}/disputes/otp/"
        requested = self.client.post(otp_path, data=json.dumps({}), content_type="application/json")
        challenge_id = requested.json()["challenge_id"]
        verified = self.client.post(
            f"{otp_path}{challenge_id}/verify/",
            data=json.dumps({"code": "123456"}),
            content_type="application/json",
        )
        dispute_token = verified.json()["dispute_token"]
        opened = self.client.post(
            f"/api/v1/checkout/{self.checkout_token}/disputes/",
            data=json.dumps({"challenge_id": challenge_id, "dispute_token": dispute_token}),
            content_type="application/json",
        )
        return challenge_id, dispute_token, opened.json()["dispute_id"]

    @patch(
        "escrow.disputes.views.check_customer_otp_verify_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch(
        "escrow.disputes.views.check_customer_otp_send_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch("escrow.delivery.services.send_customer_dispute_otp")
    @patch("escrow.delivery.services._new_otp_code", return_value="123456")
    def test_validated_evidence_is_stored_privately_and_described_in_postgres(
        self,
        _: object,
        send_email: object,
        __: object,
        ___: object,
    ) -> None:
        challenge_id, dispute_token, dispute_id = self._open_dispute()
        content = b"%PDF-1.7\nfictional purchase receipt\n"
        upload = BytesIO(content)
        upload.name = "receipt.pdf"

        with patch("escrow.disputes.views.evidence_s3_client", return_value=self.storage):
            uploaded = self.client.post(
                f"/api/v1/checkout/{self.checkout_token}/disputes/{dispute_id}/evidence/",
                data={
                    "challenge_id": challenge_id,
                    "dispute_token": dispute_token,
                    "file": upload,
                },
            )

        assert uploaded.status_code == 201
        body = uploaded.json()
        assert body["media_type"] == "application/pdf"
        assert body["size_bytes"] == len(content)
        assert body["sha256"] == hashlib.sha256(content).hexdigest()
        assert "object_key" not in body
        assert "url" not in body

        evidence = Evidence.objects.get(dispute_id=dispute_id)
        assert str(evidence.id) == body["evidence_id"]
        assert evidence.object_key.startswith(f"private/disputes/{dispute_id}/")
        assert evidence.object_key.endswith(".pdf")
        stored = self.storage.objects[("escrow-evidence", evidence.object_key)]
        assert stored == (content, "application/pdf")

        audit = AuditEvent.objects.get(event_type="evidence_uploaded")
        assert audit.payload == {"dispute_id": dispute_id, "evidence_id": str(evidence.id)}

    @patch(
        "escrow.disputes.views.check_customer_otp_verify_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch(
        "escrow.disputes.views.check_customer_otp_send_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch("escrow.delivery.services.send_customer_dispute_otp")
    @patch("escrow.delivery.services._new_otp_code", return_value="123456")
    def test_content_that_disagrees_with_its_extension_is_rejected(
        self,
        _: object,
        send_email: object,
        __: object,
        ___: object,
    ) -> None:
        challenge_id, dispute_token, dispute_id = self._open_dispute()
        upload = BytesIO(b"%PDF-1.7\nfictional purchase receipt\n")
        upload.name = "receipt.png"

        with patch("escrow.disputes.views.evidence_s3_client", return_value=self.storage):
            rejected = self.client.post(
                f"/api/v1/checkout/{self.checkout_token}/disputes/{dispute_id}/evidence/",
                data={
                    "challenge_id": challenge_id,
                    "dispute_token": dispute_token,
                    "file": upload,
                },
            )

        assert rejected.status_code == 400
        assert rejected.json()["code"] == "validation_error"
        assert Evidence.objects.count() == 0
        assert self.storage.objects == {}

    @patch(
        "escrow.disputes.views.check_customer_otp_verify_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch(
        "escrow.disputes.views.check_customer_otp_send_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch("escrow.delivery.services.send_customer_dispute_otp")
    @patch("escrow.delivery.services._new_otp_code", return_value="123456")
    def test_a_wrong_dispute_capability_cannot_upload(
        self,
        _: object,
        send_email: object,
        __: object,
        ___: object,
    ) -> None:
        challenge_id, _dispute_token, dispute_id = self._open_dispute()
        upload = BytesIO(b"%PDF-1.7\nfictional purchase receipt\n")
        upload.name = "receipt.pdf"

        with patch("escrow.disputes.views.evidence_s3_client", return_value=self.storage):
            rejected = self.client.post(
                f"/api/v1/checkout/{self.checkout_token}/disputes/{dispute_id}/evidence/",
                data={
                    "challenge_id": challenge_id,
                    "dispute_token": "otp_dispute_forged",
                    "file": upload,
                },
            )

        assert rejected.status_code == 403
        assert rejected.json()["code"] == "customer_evidence_unauthorized"
        assert Evidence.objects.count() == 0

    @patch(
        "escrow.disputes.views.check_customer_otp_verify_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch(
        "escrow.disputes.views.check_customer_otp_send_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch("escrow.delivery.services.send_customer_dispute_otp")
    @patch("escrow.delivery.services._new_otp_code", return_value="123456")
    def test_an_unknown_dispute_for_this_checkout_capability_is_not_found(
        self,
        _: object,
        send_email: object,
        __: object,
        ___: object,
    ) -> None:
        challenge_id, dispute_token, _dispute_id = self._open_dispute()
        upload = BytesIO(b"%PDF-1.7\nfictional purchase receipt\n")
        upload.name = "receipt.pdf"

        with patch("escrow.disputes.views.evidence_s3_client", return_value=self.storage):
            missing = self.client.post(
                f"/api/v1/checkout/{self.checkout_token}/disputes/{uuid4()}/evidence/",
                data={
                    "challenge_id": challenge_id,
                    "dispute_token": dispute_token,
                    "file": upload,
                },
            )

        assert missing.status_code == 404
        assert Evidence.objects.count() == 0
