from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.integrations.models import WebhookDelivery, WebhookEndpoint, WebhookEvent
from escrow.integrations.rate_limit import RateLimitDecision
from escrow.integrations.webhooks import (
    WebhookEndpointValidationError,
    create_webhook_endpoint,
    deliver_webhook_event,
    enqueue_agreement_webhook_event,
    enqueue_due_webhook_deliveries,
    replay_webhook_delivery,
    rotate_webhook_secret,
    validate_webhook_endpoint_url,
)
from escrow.messaging.models import OutboxEvent
from escrow.notifications.outbox import enqueue_agreement_status_changed
from escrow.organizations.models import Organization, OrganizationMember


def _agreement(*, organization: Organization, sequence: int = 3) -> EscrowAgreement:
    return EscrowAgreement.objects.create(
        organization=organization,
        external_customer_id="buyer-webhook-001",
        customer_name_masked="A***",
        customer_email_masked="a***@example.test",
        customer_document_masked="***.***.***-25",
        customer_document_kind="CPF",
        customer_email_blind_index="a" * 64,
        customer_document_blind_index="b" * 64,
        customer_pii_ciphertext=b"ciphertext",
        customer_pii_nonce=b"nonce",
        customer_pii_encrypted_data_key=b"encrypted-key",
        customer_pii_kms_key_id="test-key",
        checkout_token_hash=f"checkout-{uuid4().hex}",
        amount_minor=50_000,
        currency="BRL",
        fee_bps=200,
        delivery_window_days=7,
        status=EscrowAgreement.Status.HELD,
        realtime_sequence=sequence,
    )


class WebhookEndpointTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Webhook organization")

    def test_endpoint_secret_is_returned_once_and_persisted_encrypted(self) -> None:
        endpoint, secret = create_webhook_endpoint(
            organization=self.organization,
            url="https://hooks.example.test/escrow",
            resolver=lambda _: ["93.184.216.34"],
        )

        stored = WebhookEndpoint.objects.get(id=endpoint.id)

        assert secret.startswith("whsec_")
        assert secret.encode() not in bytes(stored.secret_ciphertext)
        assert stored.url == "https://hooks.example.test/escrow"
        assert stored.is_active

    def test_endpoint_validation_rejects_unsafe_schemes_private_networks_and_redirects(
        self,
    ) -> None:
        with self.assertRaises(WebhookEndpointValidationError):
            validate_webhook_endpoint_url(
                "http://hooks.example.test", resolver=lambda _: ["93.184.216.34"]
            )
        with self.assertRaises(WebhookEndpointValidationError):
            validate_webhook_endpoint_url(
                "https://127.0.0.1/hook", resolver=lambda _: ["127.0.0.1"]
            )
        with self.assertRaises(WebhookEndpointValidationError):
            validate_webhook_endpoint_url(
                "https://hooks.example.test", resolver=lambda _: ["10.0.0.8"]
            )
        with self.assertRaises(WebhookEndpointValidationError):
            validate_webhook_endpoint_url(
                "https://user:pass@hooks.example.test", resolver=lambda _: ["93.184.216.34"]
            )

    def test_rotation_keeps_short_overlap_without_revealing_old_secret(self) -> None:
        endpoint, initial_secret = create_webhook_endpoint(
            organization=self.organization,
            url="https://hooks.example.test/escrow",
            resolver=lambda _: ["93.184.216.34"],
        )

        rotated, fresh_secret = rotate_webhook_secret(endpoint, overlap_seconds=300)

        endpoint.refresh_from_db()
        assert fresh_secret.startswith("whsec_")
        assert fresh_secret != initial_secret
        assert endpoint.previous_secret_expires_at is not None
        assert endpoint.previous_secret_expires_at <= timezone.now() + timedelta(seconds=301)
        assert rotated.id == endpoint.id


class WebhookEventDeliveryTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Delivery organization")
        self.endpoint, self.secret = create_webhook_endpoint(
            organization=self.organization,
            url="https://hooks.example.test/escrow",
            resolver=lambda _: ["93.184.216.34"],
        )
        self.agreement = _agreement(organization=self.organization)
        self.rate_limiter = self.enterContext(
            patch(
                "escrow.integrations.webhooks.check_outgoing_webhook_rate_limit",
                return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
            )
        )

    def test_status_event_is_safe_versioned_and_enqueued_once(self) -> None:
        event = enqueue_agreement_webhook_event(
            self.agreement,
            correlation_id="webhook-correlation-001",
            causation_id="payment-event-001",
        )
        replay = enqueue_agreement_webhook_event(
            self.agreement,
            correlation_id="webhook-correlation-001",
            causation_id="payment-event-001",
        )

        assert event.id == replay.id
        assert WebhookEvent.objects.count() == 1
        assert (
            OutboxEvent.objects.filter(
                message_type="DeliverWebhookEvent.v1", payload={"event_id": str(event.id)}
            ).count()
            == 1
        )
        assert (
            event.payload.items()
            >= {
                "event_id": str(event.id),
                "type": "agreement.status_changed",
                "version": 1,
                "agreement_id": str(self.agreement.id),
                "sequence": 3,
                "status": "HELD",
                "correlation_id": "webhook-correlation-001",
            }.items()
        )
        assert event.payload["timestamp"].endswith("Z")
        assert "customer" not in event.payload

    def test_every_safe_status_change_also_creates_a_webhook_event(self) -> None:
        with transaction.atomic():
            enqueue_agreement_status_changed(
                self.agreement,
                correlation_id="webhook-correlation-status-001",
                causation_id="status-cause-001",
            )

        event = WebhookEvent.objects.get(agreement=self.agreement)
        assert OutboxEvent.objects.filter(message_type="AgreementStatusChanged.v1").count() == 1
        assert (
            OutboxEvent.objects.filter(
                message_type="DeliverWebhookEvent.v1",
                payload={"event_id": str(event.id)},
            ).count()
            == 1
        )

    def test_delivery_signs_exact_body_and_replayed_command_does_not_send_twice(self) -> None:
        event = enqueue_agreement_webhook_event(
            self.agreement,
            correlation_id="webhook-correlation-002",
            causation_id=None,
        )
        sender = MagicMock(return_value=204)

        first = deliver_webhook_event(event.id, sender=sender, now=timezone.now())
        second = deliver_webhook_event(event.id, sender=sender, now=timezone.now())

        delivery = WebhookDelivery.objects.get(endpoint=self.endpoint, event=event)
        headers = sender.call_args.kwargs["headers"]
        body = sender.call_args.kwargs["body"]
        signature_payload = f"{headers['X-Escrow-Timestamp']}.".encode() + body
        expected = hmac.new(self.secret.encode(), signature_payload, hashlib.sha256).hexdigest()
        assert first.delivered
        assert second.already_delivered
        assert sender.call_count == 1
        assert headers["X-Escrow-Signature"] == f"v1={expected}"
        assert delivery.status == WebhookDelivery.Status.DELIVERED
        assert delivery.attempts == 1

    def test_secret_rotation_signs_with_new_and_overlapping_previous_secret(self) -> None:
        _, fresh_secret = rotate_webhook_secret(self.endpoint, overlap_seconds=300)
        event = enqueue_agreement_webhook_event(
            self.agreement,
            correlation_id="webhook-correlation-rotation-001",
            causation_id=None,
        )
        sender = MagicMock(return_value=202)
        now = timezone.now()

        deliver_webhook_event(event.id, sender=sender, now=now)

        headers = sender.call_args.kwargs["headers"]
        body = sender.call_args.kwargs["body"]
        signed = f"{headers['X-Escrow-Timestamp']}.".encode() + body
        assert headers["X-Escrow-Signature"] == (
            "v1=" + hmac.new(fresh_secret.encode(), signed, hashlib.sha256).hexdigest()
        )
        assert headers["X-Escrow-Previous-Signature"] == (
            "v1=" + hmac.new(self.secret.encode(), signed, hashlib.sha256).hexdigest()
        )

    def test_redirect_response_is_failed_without_following_it(self) -> None:
        event = enqueue_agreement_webhook_event(
            self.agreement,
            correlation_id="webhook-correlation-redirect-001",
            causation_id=None,
        )

        result = deliver_webhook_event(
            event.id, sender=MagicMock(return_value=302), now=timezone.now()
        )

        delivery = WebhookDelivery.objects.get(endpoint=self.endpoint, event=event)
        assert result.failed
        assert delivery.status == WebhookDelivery.Status.FAILED
        assert delivery.last_error == "UnsafeRedirect"

    def test_transient_delivery_failure_schedules_bounded_retry_without_dropping_event(
        self,
    ) -> None:
        event = enqueue_agreement_webhook_event(
            self.agreement,
            correlation_id="webhook-correlation-003",
            causation_id=None,
        )
        sender = MagicMock(side_effect=TimeoutError)
        now = timezone.now()

        result = deliver_webhook_event(event.id, sender=sender, now=now)

        delivery = WebhookDelivery.objects.get(endpoint=self.endpoint, event=event)
        assert result.retry_scheduled
        assert delivery.status == WebhookDelivery.Status.RETRYING
        assert delivery.attempts == 1
        assert delivery.next_attempt_at is not None
        assert delivery.next_attempt_at > now

    def test_event_payload_carries_causation_id_for_correlation(self) -> None:
        event = enqueue_agreement_webhook_event(
            self.agreement,
            correlation_id="webhook-correlation-causation",
            causation_id="pix-callback-causation",
        )

        assert event.payload["causation_id"] == "pix-callback-causation"

    def test_sequences_increase_per_agreement_so_receivers_can_detect_gaps(self) -> None:
        first = enqueue_agreement_webhook_event(
            self.agreement,
            correlation_id="webhook-sequence-001",
            causation_id=None,
        )
        self.agreement.realtime_sequence = 4
        self.agreement.save(update_fields=["realtime_sequence"])
        second = enqueue_agreement_webhook_event(
            self.agreement,
            correlation_id="webhook-sequence-002",
            causation_id=None,
        )

        assert first.payload["sequence"] == 3
        assert second.payload["sequence"] == 4
        assert second.payload["event_id"] != first.payload["event_id"]

    def test_retries_exhausted_after_twenty_four_hours_fail_without_losing_the_event(self) -> None:
        event = enqueue_agreement_webhook_event(
            self.agreement,
            correlation_id="webhook-correlation-exhaustion",
            causation_id=None,
        )
        started = timezone.now()
        deliver_webhook_event(event.id, sender=MagicMock(side_effect=TimeoutError), now=started)
        delivery = WebhookDelivery.objects.get(endpoint=self.endpoint, event=event)

        result = deliver_webhook_event(
            event.id,
            sender=MagicMock(side_effect=TimeoutError),
            now=started + timedelta(hours=24, seconds=1),
        )

        delivery.refresh_from_db()
        assert result.failed
        assert delivery.status == WebhookDelivery.Status.FAILED
        assert delivery.next_attempt_at is None
        assert delivery.last_error == "TimeoutError"
        assert WebhookEvent.objects.filter(id=event.id).exists()

    def test_rate_limited_delivery_is_delayed_in_the_queue_not_dropped(self) -> None:
        self.rate_limiter.return_value = RateLimitDecision(  # type: ignore[attr-defined]
            allowed=False, retry_after_seconds=1
        )
        event = enqueue_agreement_webhook_event(
            self.agreement,
            correlation_id="webhook-correlation-rate-limit",
            causation_id=None,
        )
        now = timezone.now()
        sender = MagicMock(return_value=200)

        result = deliver_webhook_event(event.id, sender=sender, now=now)

        delivery = WebhookDelivery.objects.get(endpoint=self.endpoint, event=event)
        assert result.retry_scheduled
        assert sender.call_count == 0
        assert delivery.status == WebhookDelivery.Status.RETRYING
        assert delivery.attempts == 0
        assert delivery.next_attempt_at is not None
        assert delivery.next_attempt_at > now
        assert delivery.last_error == "RateLimited"

    def test_permanent_endpoint_validation_failure_is_not_retried(self) -> None:
        event = enqueue_agreement_webhook_event(
            self.agreement,
            correlation_id="webhook-correlation-permanent",
            causation_id=None,
        )
        sender = MagicMock(side_effect=WebhookEndpointValidationError("unsafe"))

        result = deliver_webhook_event(event.id, sender=sender, now=timezone.now())

        delivery = WebhookDelivery.objects.get(endpoint=self.endpoint, event=event)
        assert result.failed
        assert delivery.status == WebhookDelivery.Status.FAILED
        assert delivery.next_attempt_at is None
        assert delivery.last_error == "WebhookEndpointValidationError"

    def test_due_retry_and_manual_replay_keep_event_identity_and_create_new_commands(self) -> None:
        event = enqueue_agreement_webhook_event(
            self.agreement,
            correlation_id="webhook-correlation-004",
            causation_id=None,
        )
        now = timezone.now()
        deliver_webhook_event(event.id, sender=MagicMock(side_effect=TimeoutError), now=now)
        delivery = WebhookDelivery.objects.get(endpoint=self.endpoint, event=event)
        assert delivery.next_attempt_at is not None

        due_count = enqueue_due_webhook_deliveries(now=delivery.next_attempt_at)

        assert due_count == 1
        assert OutboxEvent.objects.filter(message_type="DeliverWebhookEvent.v1").count() == 2

        delivery.status = WebhookDelivery.Status.FAILED
        delivery.save(update_fields=["status"])
        replayed = replay_webhook_delivery(delivery, correlation_id="webhook-correlation-005")

        assert replayed.status == WebhookDelivery.Status.PENDING
        assert OutboxEvent.objects.filter(message_type="DeliverWebhookEvent.v1").count() == 3


class WebhookEndpointApiTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Webhook API organization")
        self.owner = get_user_model().objects.create_user(
            email="owner@webhooks.example",
            password="Uma senha forte e exclusiva 2026!",
        )
        self.viewer = get_user_model().objects.create_user(
            email="viewer@webhooks.example",
            password="Uma senha forte e exclusiva 2026!",
        )
        OrganizationMember.objects.create(
            organization=self.organization,
            user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        OrganizationMember.objects.create(
            organization=self.organization,
            user=self.viewer,
            role=OrganizationMember.Role.VIEWER,
        )
        self.collection_url = "/api/v1/organizations/current/webhooks/"

    def test_owner_can_create_and_list_endpoint_without_secret_redisplay(self) -> None:
        self.client.force_login(self.owner)
        with patch(
            "escrow.integrations.views.create_webhook_endpoint",
            side_effect=lambda **kwargs: create_webhook_endpoint(
                **kwargs, resolver=lambda _: ["93.184.216.34"]
            ),
        ):
            created = self.client.post(
                self.collection_url,
                data=json.dumps({"url": "https://hooks.example.test/escrow"}),
                content_type="application/json",
            )
        listed = self.client.get(self.collection_url)

        assert created.status_code == 201
        assert created.json()["secret"].startswith("whsec_")
        assert listed.status_code == 200
        assert listed.json()["webhook_endpoints"][0]["url"] == "https://hooks.example.test/escrow"
        assert "secret" not in listed.json()["webhook_endpoints"][0]

    def test_deliveries_list_exposes_attempts_errors_and_replay_count(self) -> None:
        self.client.force_login(self.owner)
        endpoint, _ = create_webhook_endpoint(
            organization=self.organization,
            url="https://hooks.example.test/escrow",
            resolver=lambda _: ["93.184.216.34"],
        )
        agreement = _agreement(organization=self.organization)
        event = enqueue_agreement_webhook_event(
            agreement,
            correlation_id="webhook-dashboard-001",
            causation_id=None,
        )
        delivery = WebhookDelivery.objects.create(
            endpoint=endpoint,
            event=event,
            status=WebhookDelivery.Status.FAILED,
            attempts=3,
            last_error="Http500",
            replay_count=2,
        )

        response = self.client.get("/api/v1/organizations/current/webhook-deliveries/")

        assert response.status_code == 200
        payload = response.json()["webhook_deliveries"][0]
        assert payload["id"] == str(delivery.id)
        assert payload["status"] == "FAILED"
        assert payload["attempts"] == 3
        assert payload["last_error"] == "Http500"
        assert payload["replay_count"] == 2

    def test_non_owner_cannot_configure_endpoint(self) -> None:
        self.client.force_login(self.viewer)

        response = self.client.post(
            self.collection_url,
            data=json.dumps({"url": "https://hooks.example.test/escrow"}),
            content_type="application/json",
        )

        assert response.status_code == 403
        assert response.json()["code"] == "organization_role_forbidden"
