from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.audit.models import AuditEvent
from escrow.disputes.models import Evidence, EvidenceAccessGrant
from escrow.disputes.services import open_dispute_after_customer_authorization
from escrow.organizations.models import Organization
from escrow.risk.services import PLATFORM_ADMIN_GROUP, RISK_DISPUTE_ANALYST_GROUP


class FakeEvidenceS3Client:
    """In-memory double for the Ceph RGW boundary used by download tests."""

    def generate_presigned_url(
        self,
        operation: str,
        *,
        Params: dict[str, str],
        ExpiresIn: int,
    ) -> str:
        return (
            f"https://rgw.local/{Params['Bucket']}/{Params['Key']}"
            f"?operation={operation}&ttl={ExpiresIn}"
        )


class EvidenceAccessApiTests(TestCase):
    def setUp(self) -> None:
        organization = Organization.objects.create(name="Loja acesso evidencia")
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
        dispute = open_dispute_after_customer_authorization(
            agreement_id=self.agreement.id,
            correlation_id="evidence-access-setup",
        ).dispute
        self.evidence = Evidence.objects.create(
            dispute=dispute,
            object_key=f"private/disputes/{dispute.id}/{uuid4()}.pdf",
            extension="pdf",
            media_type="application/pdf",
            size_bytes=42,
            sha256="a" * 64,
            uploaded_at=timezone.now(),
        )
        self.dispute = dispute
        self.analyst = get_user_model().objects.create_user(
            email="analyst@evidence-access.test",
            password="Uma senha forte e exclusiva 2026!",
            is_staff=True,
        )
        analyst_group, _ = Group.objects.get_or_create(name=RISK_DISPUTE_ANALYST_GROUP)
        self.analyst.groups.add(analyst_group)
        self.grant_url = (
            f"/api/v1/operations/disputes/{self.dispute.id}"
            f"/evidence/{self.evidence.id}/access-grants/"
        )

    def test_analyst_receives_one_time_limited_audited_download_capability(self) -> None:
        self.client.force_login(self.analyst)

        granted = self.client.post(
            self.grant_url, data=json.dumps({}), content_type="application/json"
        )

        assert granted.status_code == 201
        body = granted.json()
        assert set(body) == {"access_token", "expires_at"}
        access_token = body["access_token"]
        assert access_token.startswith("eva_")
        assert "object_key" not in body
        grant = EvidenceAccessGrant.objects.get(evidence=self.evidence)
        assert grant.actor_id == self.analyst.id
        assert access_token not in grant.token_hash

        from unittest.mock import patch

        with patch(
            "escrow.disputes.operations.evidence_s3_client",
            return_value=FakeEvidenceS3Client(),
        ):
            downloaded = self.client.get(
                f"/api/v1/operations/disputes/evidence-access/{access_token}/download/"
            )

        assert downloaded.status_code == 200
        download_body = downloaded.json()
        assert download_body["expires_at"] == body["expires_at"]
        assert self.evidence.object_key in download_body["download_url"]

        grant.refresh_from_db()
        assert grant.last_accessed_at is not None
        events = AuditEvent.objects.filter(
            event_type__in=["evidence_access_granted", "evidence_accessed"]
        )
        assert [event.event_type for event in events] == [
            "evidence_access_granted",
            "evidence_accessed",
        ]

    def test_platform_admin_can_also_receive_a_grant(self) -> None:
        admin = get_user_model().objects.create_user(
            email="admin@evidence-access.test",
            password="Uma senha forte e exclusiva 2026!",
            is_staff=True,
        )
        admin_group, _ = Group.objects.get_or_create(name=PLATFORM_ADMIN_GROUP)
        admin.groups.add(admin_group)
        self.client.force_login(admin)

        granted = self.client.post(
            self.grant_url, data=json.dumps({}), content_type="application/json"
        )

        assert granted.status_code == 201

    def test_non_staff_users_and_anonymous_callers_cannot_receive_grants(self) -> None:
        outsider = get_user_model().objects.create_user(
            email="outsider@evidence-access.test",
            password="Uma senha forte e exclusiva 2026!",
        )
        self.client.force_login(outsider)
        forbidden = self.client.post(
            self.grant_url, data=json.dumps({}), content_type="application/json"
        )

        self.client.logout()
        anonymous = self.client.post(
            self.grant_url, data=json.dumps({}), content_type="application/json"
        )

        assert forbidden.status_code == 403
        assert forbidden.json()["code"] == "evidence_access_forbidden"
        assert anonymous.status_code == 401
        assert EvidenceAccessGrant.objects.count() == 0

    def test_an_expired_grant_no_longer_authorizes_a_download(self) -> None:
        self.client.force_login(self.analyst)
        issued_at = timezone.now() - timedelta(hours=1)
        from escrow.disputes.services import issue_evidence_access_grant

        grant, access_token = issue_evidence_access_grant(
            dispute_id=self.dispute.id,
            evidence_id=self.evidence.id,
            actor=self.analyst,
            correlation_id="evidence-access-expired",
            now=issued_at,
        )
        assert grant.expires_at < timezone.now()

        from unittest.mock import patch

        with patch(
            "escrow.disputes.operations.evidence_s3_client",
            return_value=FakeEvidenceS3Client(),
        ):
            expired = self.client.get(
                f"/api/v1/operations/disputes/evidence-access/{access_token}/download/"
            )

        assert expired.status_code == 410
        assert expired.json()["code"] == "evidence_access_expired"

    def test_an_unknown_access_token_is_not_found(self) -> None:
        missing = self.client.get(
            "/api/v1/operations/disputes/evidence-access/eva_forged/download/"
        )

        assert missing.status_code == 404
