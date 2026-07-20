from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.pii import CustomerIdentity, envelope_cipher
from escrow.agreements.services import checkout_token_hash, customer_pii_context
from escrow.audit.models import AuditEvent
from escrow.delivery.services import (
    CustomerOtpStateConflict,
    accept_customer_delivery,
    enqueue_expired_inspection_releases,
    request_customer_acceptance_otp,
    verify_customer_acceptance_otp,
)
from escrow.disputes.services import DisputeStateConflict, open_dispute_after_customer_authorization
from escrow.integrations.models import WebhookEvent
from escrow.integrations.rate_limit import RateLimitDecision
from escrow.ledger.models import LedgerEntry, LedgerTransaction
from escrow.ledger.services import LedgerEntryInput, LedgerPosting, post_ledger_transaction
from escrow.ledger.tasks import release_funds
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.models import OutboxEvent
from escrow.organizations.models import Organization
from escrow.payments.models import Transfer

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.get_default_timezone())


def _agreement(
    *,
    status: str = EscrowAgreement.Status.INSPECTION,
    inspection_deadline_at: datetime | None = None,
    checkout_token: str | None = None,
) -> EscrowAgreement:
    organization = Organization.objects.create(name=f"Expired release {uuid4().hex}")
    token_hash = (
        checkout_token_hash(checkout_token) if checkout_token is not None else uuid4().hex * 2
    )
    agreement = EscrowAgreement.objects.create(
        organization=organization,
        external_customer_id=f"expired-{uuid4().hex}",
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
        checkout_token_hash=token_hash,
        amount_minor=50_000,
        currency="BRL",
        fee_bps=200,
        delivery_window_days=7,
        funding_confirmed_at=NOW - timedelta(days=14),
        delivery_due_at=NOW - timedelta(days=7),
        inspection_deadline_at=inspection_deadline_at,
        status=status,
        realtime_sequence=2,
    )
    if checkout_token is not None:
        agreement.checkout_token = checkout_token
    return agreement


class ExpiredInspectionReleaseScanTests(TestCase):
    def test_expired_inspection_enters_release_pending_with_exactly_one_command(self) -> None:
        agreement = _agreement(inspection_deadline_at=NOW - timedelta(seconds=1))

        enqueued = enqueue_expired_inspection_releases(now=NOW)

        assert enqueued == 1
        agreement.refresh_from_db()
        assert agreement.status == EscrowAgreement.Status.RELEASE_PENDING
        transfer = Transfer.objects.get(agreement=agreement, kind=Transfer.Kind.RELEASE)
        assert transfer.status == Transfer.Status.PENDING
        assert transfer.amount_minor == agreement.amount_minor
        assert transfer.currency == agreement.currency
        command = OutboxEvent.objects.get(message_type="ReleaseFunds.v1")
        assert command.routing_key == "ledger.release"
        assert command.payload == {
            "agreement_id": str(agreement.id),
            "transfer_id": str(transfer.id),
        }
        status_event = OutboxEvent.objects.get(message_type="AgreementStatusChanged.v1")
        assert status_event.payload["status"] == EscrowAgreement.Status.RELEASE_PENDING
        webhook_event = WebhookEvent.objects.get(agreement=agreement)
        assert webhook_event.payload["status"] == EscrowAgreement.Status.RELEASE_PENDING
        assert AuditEvent.objects.filter(
            agreement=agreement, event_type="inspection_expired_release_enqueued"
        ).exists()

    def test_deadline_at_the_scan_instant_is_expired(self) -> None:
        agreement = _agreement(inspection_deadline_at=NOW)

        enqueued = enqueue_expired_inspection_releases(now=NOW)

        assert enqueued == 1
        agreement.refresh_from_db()
        assert agreement.status == EscrowAgreement.Status.RELEASE_PENDING

    def test_deadline_one_second_in_the_future_is_not_expired(self) -> None:
        agreement = _agreement(inspection_deadline_at=NOW + timedelta(seconds=1))

        enqueued = enqueue_expired_inspection_releases(now=NOW)

        assert enqueued == 0
        agreement.refresh_from_db()
        assert agreement.status == EscrowAgreement.Status.INSPECTION
        assert not Transfer.objects.filter(agreement=agreement).exists()

    def test_non_inspection_and_terminal_agreements_are_ignored_safely(self) -> None:
        past = NOW - timedelta(days=1)
        ignored = [
            (_agreement(status=s, inspection_deadline_at=d), s)
            for s, d in [
                (EscrowAgreement.Status.AWAITING_PAYMENT, None),
                (EscrowAgreement.Status.FUNDING_PROCESSING, past),
                (EscrowAgreement.Status.HELD, past),
                (EscrowAgreement.Status.REVIEW_REQUIRED, past),
                (EscrowAgreement.Status.FUNDING_REJECTED, past),
                (EscrowAgreement.Status.DISPUTED, past),
                (EscrowAgreement.Status.RELEASE_PENDING, past),
                (EscrowAgreement.Status.RELEASED, past),
                (EscrowAgreement.Status.REFUND_PENDING, past),
                (EscrowAgreement.Status.REFUNDED, past),
                (EscrowAgreement.Status.CANCELLED, past),
                (EscrowAgreement.Status.INSPECTION, None),
            ]
        ]

        enqueued = enqueue_expired_inspection_releases(now=NOW)

        assert enqueued == 0
        for agreement, expected_status in ignored:
            agreement.refresh_from_db()
            assert agreement.status == expected_status
        assert not Transfer.objects.filter(kind=Transfer.Kind.RELEASE).exists()
        assert not OutboxEvent.objects.filter(message_type="ReleaseFunds.v1").exists()

    def test_repeated_scans_never_duplicate_the_release(self) -> None:
        agreement = _agreement(inspection_deadline_at=NOW - timedelta(seconds=1))

        first = enqueue_expired_inspection_releases(now=NOW)
        second = enqueue_expired_inspection_releases(now=NOW + timedelta(seconds=30))

        assert (first, second) == (1, 0)
        assert Transfer.objects.filter(agreement=agreement, kind=Transfer.Kind.RELEASE).count() == 1
        assert OutboxEvent.objects.filter(message_type="ReleaseFunds.v1").count() == 1
        assert (
            WebhookEvent.objects.filter(
                agreement=agreement,
                payload__status=EscrowAgreement.Status.RELEASE_PENDING,
            ).count()
            == 1
        )


def _checkout_agreement_in_inspection(
    *,
    checkout_token: str,
    inspection_deadline_at: datetime,
) -> EscrowAgreement:
    organization = Organization.objects.create(name=f"Release race {uuid4().hex}")
    customer = CustomerIdentity(
        name="Ana Compradora",
        email="buyer@example.test",
        document="52998224725",
        document_kind="CPF",
    )
    agreement_id = uuid4()
    encrypted = envelope_cipher().encrypt(
        customer.plaintext(),
        customer_pii_context(organization.id, agreement_id),
    )
    agreement = EscrowAgreement.objects.create(
        id=agreement_id,
        organization=organization,
        external_customer_id=f"race-{uuid4().hex}",
        customer_name_masked="Ana C.",
        customer_email_masked="b***@example.test",
        customer_document_masked="***.***.***-25",
        customer_document_kind="CPF",
        customer_email_blind_index=uuid4().hex * 2,
        customer_document_blind_index=uuid4().hex * 2,
        customer_pii_ciphertext=encrypted.ciphertext,
        customer_pii_nonce=encrypted.nonce,
        customer_pii_encrypted_data_key=encrypted.encrypted_data_key,
        customer_pii_kms_key_id=encrypted.kms_key_id,
        checkout_token_hash=checkout_token_hash(checkout_token),
        amount_minor=50_000,
        currency="BRL",
        fee_bps=200,
        delivery_window_days=7,
        funding_confirmed_at=NOW - timedelta(days=14),
        delivery_due_at=NOW - timedelta(days=7),
        inspection_deadline_at=inspection_deadline_at,
        status=EscrowAgreement.Status.INSPECTION,
        realtime_sequence=2,
    )
    agreement.checkout_token = checkout_token
    return agreement


class ExpiredInspectionReleaseRaceTests(TestCase):
    def _authorized_acceptance(
        self,
        agreement: EscrowAgreement,
        *,
        at: datetime,
    ) -> tuple[str, str]:
        with (
            patch("escrow.delivery.services.send_customer_acceptance_otp"),
            patch("escrow.delivery.services._new_otp_code", return_value="123456"),
        ):
            requested = request_customer_acceptance_otp(
                checkout_token=agreement.checkout_token,
                correlation_id="race-otp-request",
                now=at,
            )
        verified = verify_customer_acceptance_otp(
            checkout_token=agreement.checkout_token,
            challenge_id=requested.challenge.id,
            code="123456",
            now=at,
        )
        return str(requested.challenge.id), verified.acceptance_token

    def test_customer_acceptance_winning_the_row_lock_prevents_the_release(self) -> None:
        deadline = NOW + timedelta(seconds=1)
        agreement = _checkout_agreement_in_inspection(
            checkout_token="chk_release-race-acceptance-wins",
            inspection_deadline_at=deadline,
        )
        challenge_id, acceptance_token = self._authorized_acceptance(agreement, at=NOW)

        result = accept_customer_delivery(
            checkout_token=agreement.checkout_token,
            challenge_id=challenge_id,
            acceptance_token=acceptance_token,
            correlation_id="race-acceptance-wins",
            now=NOW,
        )

        assert not result.replayed
        enqueued = enqueue_expired_inspection_releases(now=deadline)
        assert enqueued == 0
        assert Transfer.objects.filter(agreement=agreement, kind=Transfer.Kind.RELEASE).count() == 1
        assert OutboxEvent.objects.filter(message_type="ReleaseFunds.v1").count() == 1

    def test_a_late_customer_acceptance_receives_a_state_conflict(self) -> None:
        agreement = _checkout_agreement_in_inspection(
            checkout_token="chk_release-race-scheduler-wins",
            inspection_deadline_at=NOW,
        )
        challenge_id, acceptance_token = self._authorized_acceptance(
            agreement, at=NOW - timedelta(seconds=1)
        )
        assert enqueue_expired_inspection_releases(now=NOW) == 1

        with self.assertRaises(CustomerOtpStateConflict):
            accept_customer_delivery(
                checkout_token=agreement.checkout_token,
                challenge_id=challenge_id,
                acceptance_token=acceptance_token,
                correlation_id="race-acceptance-loses",
                now=NOW + timedelta(seconds=1),
            )

        assert Transfer.objects.filter(agreement=agreement, kind=Transfer.Kind.RELEASE).count() == 1

    def test_a_dispute_winning_the_row_lock_prevents_the_release(self) -> None:
        deadline = NOW + timedelta(seconds=1)
        agreement = _checkout_agreement_in_inspection(
            checkout_token="chk_release-race-dispute-wins",
            inspection_deadline_at=deadline,
        )

        open_dispute_after_customer_authorization(
            agreement_id=agreement.id,
            correlation_id="race-dispute-wins",
            now=NOW,
        )

        enqueued = enqueue_expired_inspection_releases(now=deadline)
        assert enqueued == 0
        agreement.refresh_from_db()
        assert agreement.status == EscrowAgreement.Status.DISPUTED
        assert not Transfer.objects.filter(agreement=agreement, kind=Transfer.Kind.RELEASE).exists()

    def test_a_late_dispute_receives_a_state_conflict(self) -> None:
        agreement = _checkout_agreement_in_inspection(
            checkout_token="chk_release-race-dispute-loses",
            inspection_deadline_at=NOW,
        )
        assert enqueue_expired_inspection_releases(now=NOW) == 1

        with self.assertRaises(DisputeStateConflict):
            open_dispute_after_customer_authorization(
                agreement_id=agreement.id,
                correlation_id="race-dispute-loses",
                now=NOW + timedelta(seconds=1),
            )

        agreement.refresh_from_db()
        assert agreement.status == EscrowAgreement.Status.RELEASE_PENDING


def _envelope_from_outbox(event: OutboxEvent) -> MessageEnvelope:
    return MessageEnvelope.build(
        message_id=event.id,
        message_type=event.message_type,
        version=event.version,
        occurred_at=event.occurred_at,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        tenant_id=event.tenant_id,
        payload=event.payload,
    )


class ExpiredInspectionReleasePostingTests(TestCase):
    def test_release_command_posts_the_same_balanced_entries_exactly_once(self) -> None:
        agreement = _agreement(inspection_deadline_at=NOW - timedelta(seconds=1))
        funding = Transfer.objects.create(
            agreement=agreement,
            kind=Transfer.Kind.FUNDING,
            status=Transfer.Status.COMPLETED,
            amount_minor=agreement.amount_minor,
            currency=agreement.currency,
            provider=Transfer.Provider.SANDBOX_PIX,
            provider_reference=f"funding-{agreement.id}",
            idempotency_key=f"funding-{agreement.id}",
        )
        post_ledger_transaction(
            LedgerPosting(
                transfer_id=funding.id,
                kind=LedgerTransaction.Kind.FUNDS_HELD,
                currency=agreement.currency,
                idempotency_key=f"held-{agreement.id}",
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
        enqueue_expired_inspection_releases(now=NOW)
        command = OutboxEvent.objects.get(message_type="ReleaseFunds.v1")
        envelope = _envelope_from_outbox(command)

        release_funds.apply(args=[envelope.to_dict()]).get()
        release_funds.apply(args=[envelope.to_dict()]).get()

        agreement.refresh_from_db()
        assert agreement.status == EscrowAgreement.Status.RELEASED
        release = Transfer.objects.get(agreement=agreement, kind=Transfer.Kind.RELEASE)
        assert release.status == Transfer.Status.COMPLETED
        released = LedgerTransaction.objects.get(kind=LedgerTransaction.Kind.FUNDS_RELEASED)
        entries = LedgerEntry.objects.filter(ledger_transaction=released)
        assert set(entries.values_list("account__code", "debit_minor", "credit_minor")) == {
            ("ESCROW_LIABILITY", 50_000, 0),
            ("ORGANIZATION_PAYABLE", 0, 49_000),
            ("PLATFORM_FEE_REVENUE", 0, 1_000),
        }
        assert (
            LedgerTransaction.objects.filter(kind=LedgerTransaction.Kind.FUNDS_RELEASED).count()
            == 1
        )
        status_events = OutboxEvent.objects.filter(
            message_type="AgreementStatusChanged.v1",
        )
        assert status_events.filter(payload__status=EscrowAgreement.Status.RELEASED).count() == 1
        assert AuditEvent.objects.filter(
            agreement=agreement, event_type="funds_released"
        ).exists()


class ExpiredInspectionReleaseReasonTests(TestCase):
    def _checkout_payload(self, agreement: EscrowAgreement) -> dict[str, object]:
        allowed = RateLimitDecision(allowed=True, retry_after_seconds=0)
        with patch(
            "escrow.agreements.views.check_public_checkout_rate_limit",
            return_value=allowed,
        ):
            response = self.client.get(f"/api/v1/checkout/{agreement.checkout_token}/")
        assert response.status_code == 200
        return response.json()["agreement"]

    def test_automatic_release_is_distinguishable_in_checkout_and_webhook(self) -> None:
        agreement = _checkout_agreement_in_inspection(
            checkout_token="chk_expired-release-reason-auto",
            inspection_deadline_at=NOW - timedelta(seconds=1),
        )

        enqueue_expired_inspection_releases(now=NOW)
        command = OutboxEvent.objects.get(message_type="ReleaseFunds.v1")
        release_funds.apply(args=[_envelope_from_outbox(command).to_dict()]).get()

        payload = self._checkout_payload(agreement)
        assert payload["status"] == EscrowAgreement.Status.RELEASED
        assert payload["release_reason"] == "INSPECTION_WINDOW_EXPIRED"
        pending_event = WebhookEvent.objects.get(
            agreement=agreement, payload__status=EscrowAgreement.Status.RELEASE_PENDING
        )
        assert pending_event.payload["release_reason"] == "INSPECTION_WINDOW_EXPIRED"
        released_event = WebhookEvent.objects.get(
            agreement=agreement, payload__status=EscrowAgreement.Status.RELEASED
        )
        assert released_event.payload["release_reason"] == "INSPECTION_WINDOW_EXPIRED"
        assert AuditEvent.objects.filter(
            agreement=agreement, event_type="inspection_expired_release_enqueued"
        ).exists()
        assert not AuditEvent.objects.filter(
            agreement=agreement, event_type="customer_delivery_accepted"
        ).exists()

    def test_customer_acceptance_is_distinguishable_in_checkout_and_webhook(self) -> None:
        agreement = _checkout_agreement_in_inspection(
            checkout_token="chk_expired-release-reason-manual",
            inspection_deadline_at=NOW + timedelta(days=7),
        )
        with (
            patch("escrow.delivery.services.send_customer_acceptance_otp"),
            patch("escrow.delivery.services._new_otp_code", return_value="123456"),
        ):
            requested = request_customer_acceptance_otp(
                checkout_token=agreement.checkout_token,
                correlation_id="reason-otp-request",
                now=NOW,
            )
        verified = verify_customer_acceptance_otp(
            checkout_token=agreement.checkout_token,
            challenge_id=requested.challenge.id,
            code="123456",
            now=NOW,
        )
        accept_customer_delivery(
            checkout_token=agreement.checkout_token,
            challenge_id=requested.challenge.id,
            acceptance_token=verified.acceptance_token,
            correlation_id="reason-acceptance",
            now=NOW,
        )
        command = OutboxEvent.objects.get(message_type="ReleaseFunds.v1")
        release_funds.apply(args=[_envelope_from_outbox(command).to_dict()]).get()

        payload = self._checkout_payload(agreement)
        assert payload["status"] == EscrowAgreement.Status.RELEASED
        assert payload["release_reason"] == "CUSTOMER_ACCEPTANCE"
        released_event = WebhookEvent.objects.get(
            agreement=agreement, payload__status=EscrowAgreement.Status.RELEASED
        )
        assert released_event.payload["release_reason"] == "CUSTOMER_ACCEPTANCE"
        assert AuditEvent.objects.filter(
            agreement=agreement, event_type="customer_delivery_accepted"
        ).exists()
        assert not AuditEvent.objects.filter(
            agreement=agreement, event_type="inspection_expired_release_enqueued"
        ).exists()

    def test_an_open_inspection_has_no_release_reason(self) -> None:
        agreement = _checkout_agreement_in_inspection(
            checkout_token="chk_expired-release-reason-open",
            inspection_deadline_at=NOW + timedelta(days=7),
        )

        payload = self._checkout_payload(agreement)

        assert payload["status"] == EscrowAgreement.Status.INSPECTION
        assert payload["release_reason"] is None
