"""Secure, at-least-once outgoing webhook lifecycle for organization integrations."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import redis
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from escrow.agreements.models import EscrowAgreement
from escrow.agreements.pii import EncryptedValue, envelope_cipher
from escrow.audit.services import record_audit_event
from escrow.integrations.models import WebhookDelivery, WebhookEndpoint, WebhookEvent
from escrow.integrations.rate_limit import check_outgoing_webhook_rate_limit
from escrow.messaging.envelope import MessageEnvelope
from escrow.messaging.outbox import enqueue_outbox_event
from escrow.messaging.topology import NOTIFICATIONS_WEBHOOK_QUEUE
from escrow.organizations.models import Organization

_WEBHOOK_EVENT_NAMESPACE = uuid.UUID("9decc391-6b2b-4d3f-87ce-8f04242d92e5")
_DELIVERY_COMMAND_NAMESPACE = uuid.UUID("65b37b33-5f4e-4fd8-b998-bc0341ecefcc")
_SECRET_PREFIX = "whsec_"

Resolver = Callable[[str], Sequence[str]]
WebhookSender = Callable[..., int]


class WebhookEndpointValidationError(ValueError):
    """An endpoint could create an SSRF or transport-safety risk."""


class WebhookSecretUnavailable(RuntimeError):
    """Encrypted signing material could not be safely decrypted."""


@dataclass(frozen=True)
class WebhookDeliveryResult:
    delivered: bool = False
    retry_scheduled: bool = False
    already_delivered: bool = False
    failed: bool = False


def validate_webhook_endpoint_url(
    value: object,
    *,
    resolver: Resolver | None = None,
) -> str:
    """Allow only public HTTPS endpoints, resolving DNS before persistence."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise WebhookEndpointValidationError("webhook endpoint is invalid")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as error:
        raise WebhookEndpointValidationError("webhook endpoint is invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise WebhookEndpointValidationError("webhook endpoint is unsafe")
    host = parsed.hostname.rstrip(".").casefold()
    if host in {"localhost", "localhost.localdomain"}:
        raise WebhookEndpointValidationError("webhook endpoint is unsafe")
    addresses = list((resolver or _resolve_host)(host))
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise WebhookEndpointValidationError("webhook endpoint is unsafe")
    return parsed.geturl()


def create_webhook_endpoint(
    *,
    organization: Organization,
    url: object,
    resolver: Resolver | None = None,
) -> tuple[WebhookEndpoint, str]:
    """Create a destination and return raw signing secret exactly once."""
    normalized_url = validate_webhook_endpoint_url(url, resolver=resolver)
    endpoint_id = uuid.uuid4()
    raw_secret = _new_signing_secret()
    encrypted = envelope_cipher().encrypt(
        raw_secret.encode(), _secret_context(organization.id, endpoint_id, "current")
    )
    endpoint = WebhookEndpoint.objects.create(
        id=endpoint_id,
        organization=organization,
        url=normalized_url,
        secret_ciphertext=encrypted.ciphertext,
        secret_nonce=encrypted.nonce,
        secret_encrypted_data_key=encrypted.encrypted_data_key,
        secret_kms_key_id=encrypted.kms_key_id,
    )
    return endpoint, raw_secret


def rotate_webhook_secret(
    endpoint: WebhookEndpoint,
    *,
    overlap_seconds: int,
    now: datetime | None = None,
) -> tuple[WebhookEndpoint, str]:
    """Rotate one secret while retaining a bounded verifier-overlap period."""
    if type(overlap_seconds) is not int or not 0 <= overlap_seconds <= 86_400:
        raise WebhookEndpointValidationError("webhook overlap is invalid")
    current_time = now or timezone.now()
    with transaction.atomic():
        locked = WebhookEndpoint.objects.select_for_update().get(id=endpoint.id)
        raw_secret = _new_signing_secret()
        encrypted = envelope_cipher().encrypt(
            raw_secret.encode(), _secret_context(locked.organization_id, locked.id, "current")
        )
        locked.previous_secret_ciphertext = locked.secret_ciphertext
        locked.previous_secret_nonce = locked.secret_nonce
        locked.previous_secret_encrypted_data_key = locked.secret_encrypted_data_key
        locked.previous_secret_kms_key_id = locked.secret_kms_key_id
        locked.previous_secret_expires_at = current_time + timedelta(seconds=overlap_seconds)
        locked.secret_ciphertext = encrypted.ciphertext
        locked.secret_nonce = encrypted.nonce
        locked.secret_encrypted_data_key = encrypted.encrypted_data_key
        locked.secret_kms_key_id = encrypted.kms_key_id
        locked.save(
            update_fields=[
                "previous_secret_ciphertext",
                "previous_secret_nonce",
                "previous_secret_encrypted_data_key",
                "previous_secret_kms_key_id",
                "previous_secret_expires_at",
                "secret_ciphertext",
                "secret_nonce",
                "secret_encrypted_data_key",
                "secret_kms_key_id",
                "updated_at",
            ]
        )
        return locked, raw_secret


def enqueue_agreement_webhook_event(
    agreement: EscrowAgreement,
    *,
    correlation_id: str,
    causation_id: str | None,
    occurred_at: datetime | None = None,
) -> WebhookEvent:
    """Persist one safe status event and its delivery command in the caller transaction."""
    if agreement.realtime_sequence < 1:
        raise ValueError("webhook events need a positive agreement sequence")
    event_type = "agreement.status_changed"
    event_id = uuid.uuid5(
        _WEBHOOK_EVENT_NAMESPACE,
        f"{agreement.id}:{event_type}:{agreement.realtime_sequence}",
    )
    event_time = occurred_at or timezone.now()
    payload = {
        "event_id": str(event_id),
        "type": event_type,
        "version": 1,
        "agreement_id": str(agreement.id),
        "sequence": agreement.realtime_sequence,
        "status": agreement.status,
        "timestamp": _isoformat(event_time),
        "correlation_id": correlation_id,
        "causation_id": causation_id,
    }
    with transaction.atomic():
        event, created = WebhookEvent.objects.get_or_create(
            id=event_id,
            defaults={
                "organization_id": agreement.organization_id,
                "agreement_id": agreement.id,
                "event_type": event_type,
                "version": 1,
                "sequence": agreement.realtime_sequence,
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "payload": payload,
                "occurred_at": event_time,
            },
        )
        if created:
            _enqueue_delivery_command(event, causation_id=causation_id, command_key="initial")
        return event


def enqueue_pending_webhook_delivery(
    event: WebhookEvent,
    *,
    causation_id: str | None,
    command_key: str,
) -> None:
    """Queue a later attempt with a new command ID while retaining event identity."""
    _enqueue_delivery_command(event, causation_id=causation_id, command_key=command_key)


def enqueue_due_webhook_deliveries(*, now: datetime | None = None, limit: int = 100) -> int:
    """Queue due retries from PostgreSQL; no long-lived broker ETA state is trusted."""
    if type(limit) is not int or not 1 <= limit <= 1_000:
        raise ValueError("webhook retry scan limit is invalid")
    current_time = now or timezone.now()
    with transaction.atomic():
        events = list(
            WebhookEvent.objects.select_for_update(skip_locked=True)
            .filter(
                deliveries__status=WebhookDelivery.Status.RETRYING,
                deliveries__next_attempt_at__lte=current_time,
            )
            .distinct()
            .order_by("occurred_at", "id")[:limit]
        )
        for event in events:
            _enqueue_delivery_command(
                event,
                causation_id=str(event.id),
                command_key=_retry_command_key(event, current_time),
            )
    return len(events)


def deliver_webhook_event(
    event_id: uuid.UUID,
    *,
    sender: WebhookSender | None = None,
    now: datetime | None = None,
) -> WebhookDeliveryResult:
    """Attempt every due endpoint delivery; HTTP only happens after a durable claim."""
    current_time = now or timezone.now()
    event = WebhookEvent.objects.select_related("organization", "agreement").get(id=event_id)
    result = WebhookDeliveryResult()
    for endpoint in WebhookEndpoint.objects.filter(
        organization_id=event.organization_id, is_active=True
    ).order_by("id"):
        one_result = _deliver_to_endpoint(
            event,
            endpoint,
            sender=sender or _send_https_webhook,
            now=current_time,
        )
        result = WebhookDeliveryResult(
            delivered=result.delivered or one_result.delivered,
            retry_scheduled=result.retry_scheduled or one_result.retry_scheduled,
            already_delivered=result.already_delivered or one_result.already_delivered,
            failed=result.failed or one_result.failed,
        )
    return result


def replay_webhook_delivery(delivery: WebhookDelivery, *, correlation_id: str) -> WebhookDelivery:
    """Make a terminal failed delivery eligible again without creating a new event."""
    with transaction.atomic():
        locked = (
            WebhookDelivery.objects.select_for_update().select_related("event").get(id=delivery.id)
        )
        if locked.status != WebhookDelivery.Status.FAILED:
            return locked
        event = WebhookEvent.objects.select_for_update().get(id=locked.event_id)
        locked.status = WebhookDelivery.Status.PENDING
        locked.next_attempt_at = timezone.now()
        locked.last_error = ""
        locked.replay_count += 1
        locked.save(
            update_fields=[
                "status",
                "next_attempt_at",
                "last_error",
                "replay_count",
                "updated_at",
            ]
        )
        enqueue_pending_webhook_delivery(
            event,
            causation_id=str(locked.id),
            command_key=f"replay:{locked.id}:{locked.replay_count}",
        )
        record_audit_event(
            event_type="webhook_delivery_replayed",
            organization=locked.event.organization,
            agreement=locked.event.agreement,
            correlation_id=correlation_id,
            payload={"delivery_id": str(locked.id), "event_id": str(locked.event_id)},
        )
        return locked


def _deliver_to_endpoint(
    event: WebhookEvent,
    endpoint: WebhookEndpoint,
    *,
    sender: WebhookSender,
    now: datetime,
) -> WebhookDeliveryResult:
    with transaction.atomic():
        delivery, _ = WebhookDelivery.objects.select_for_update().get_or_create(
            endpoint=endpoint,
            event=event,
        )
        if delivery.status == WebhookDelivery.Status.DELIVERED:
            return WebhookDeliveryResult(already_delivered=True)
        if delivery.status == WebhookDelivery.Status.FAILED:
            return WebhookDeliveryResult(failed=True)
        if delivery.next_attempt_at is not None and delivery.next_attempt_at > now:
            return WebhookDeliveryResult(retry_scheduled=True)
        try:
            rate_decision = check_outgoing_webhook_rate_limit(
                str(endpoint.organization_id), _endpoint_host(endpoint.url)
            )
        except redis.RedisError:
            return _schedule_retry(delivery, now=now, error="RateLimitUnavailable")
        if not rate_decision.allowed:
            delivery.status = WebhookDelivery.Status.RETRYING
            delivery.next_attempt_at = now + timedelta(
                seconds=rate_decision.retry_after_seconds or 1
            )
            delivery.last_error = "RateLimited"
            delivery.save(update_fields=["status", "next_attempt_at", "last_error", "updated_at"])
            return WebhookDeliveryResult(retry_scheduled=True)
        secret = _decrypt_current_secret(endpoint)
        body = _canonical_payload(event.payload)
        headers = _delivery_headers(event, endpoint, secret, body, now)
        delivery.attempts += 1
        delivery.first_attempt_at = delivery.first_attempt_at or now
        delivery.last_attempt_at = now
        delivery.save(
            update_fields=["attempts", "first_attempt_at", "last_attempt_at", "updated_at"]
        )
        try:
            response_status = sender(
                url=endpoint.url,
                body=body,
                headers=headers,
                timeout=settings.WEBHOOK_DELIVERY_TIMEOUT_SECONDS,
            )
        except WebhookEndpointValidationError as error:
            delivery.status = WebhookDelivery.Status.FAILED
            delivery.next_attempt_at = None
            delivery.last_error = type(error).__name__
            delivery.save(
                update_fields=["status", "next_attempt_at", "last_error", "updated_at"]
            )
            return WebhookDeliveryResult(failed=True)
        except Exception as error:
            return _schedule_retry(delivery, now=now, error=type(error).__name__)
        if 200 <= response_status < 300:
            delivery.status = WebhookDelivery.Status.DELIVERED
            delivery.delivered_at = now
            delivery.next_attempt_at = None
            delivery.last_response_status = response_status
            delivery.last_error = ""
            delivery.save(
                update_fields=[
                    "status",
                    "delivered_at",
                    "next_attempt_at",
                    "last_response_status",
                    "last_error",
                    "updated_at",
                ]
            )
            return WebhookDeliveryResult(delivered=True)
        if 300 <= response_status < 400:
            delivery.status = WebhookDelivery.Status.FAILED
            delivery.next_attempt_at = None
            delivery.last_response_status = response_status
            delivery.last_error = "UnsafeRedirect"
            delivery.save(
                update_fields=[
                    "status",
                    "next_attempt_at",
                    "last_response_status",
                    "last_error",
                    "updated_at",
                ]
            )
            return WebhookDeliveryResult(failed=True)
        if 400 <= response_status < 500 and response_status not in {408, 429}:
            delivery.status = WebhookDelivery.Status.FAILED
            delivery.next_attempt_at = None
            delivery.last_response_status = response_status
            delivery.last_error = f"Http{response_status}"
            delivery.save(
                update_fields=[
                    "status",
                    "next_attempt_at",
                    "last_response_status",
                    "last_error",
                    "updated_at",
                ]
            )
            return WebhookDeliveryResult(failed=True)
        delivery.last_response_status = response_status
        delivery.save(update_fields=["last_response_status", "updated_at"])
        return _schedule_retry(delivery, now=now, error=f"Http{response_status}")


def _schedule_retry(
    delivery: WebhookDelivery,
    *,
    now: datetime,
    error: str,
) -> WebhookDeliveryResult:
    started_at = delivery.first_attempt_at or now
    if now >= started_at + timedelta(seconds=settings.WEBHOOK_DELIVERY_MAX_AGE_SECONDS):
        delivery.status = WebhookDelivery.Status.FAILED
        delivery.next_attempt_at = None
        delivery.last_error = error
        delivery.save(update_fields=["status", "next_attempt_at", "last_error", "updated_at"])
        return WebhookDeliveryResult(failed=True)
    delay = min(2 ** max(0, delivery.attempts - 1), 3600)
    delivery.status = WebhookDelivery.Status.RETRYING
    delivery.next_attempt_at = now + timedelta(seconds=delay)
    delivery.last_error = error
    delivery.save(update_fields=["status", "next_attempt_at", "last_error", "updated_at"])
    return WebhookDeliveryResult(retry_scheduled=True)


def _enqueue_delivery_command(
    event: WebhookEvent,
    *,
    causation_id: str | None,
    command_key: str,
) -> None:
    if event.delivery_command_key == command_key:
        return
    event.delivery_command_key = command_key
    event.save(update_fields=["delivery_command_key"])
    message_id = uuid.uuid5(_DELIVERY_COMMAND_NAMESPACE, f"{event.id}:{command_key}")
    if _outbox_event_exists(message_id):
        return
    enqueue_outbox_event(
        MessageEnvelope.build(
            message_id=message_id,
            message_type="DeliverWebhookEvent.v1",
            version=1,
            occurred_at=timezone.now(),
            correlation_id=event.correlation_id,
            causation_id=causation_id,
            tenant_id=str(event.organization_id),
            payload={"event_id": str(event.id)},
        ),
        routing_key=NOTIFICATIONS_WEBHOOK_QUEUE.name,
    )


def _retry_command_key(event: WebhookEvent, now: datetime) -> str:
    due_deliveries = event.deliveries.filter(
        status=WebhookDelivery.Status.RETRYING,
        next_attempt_at__lte=now,
    ).order_by("id")
    components = [
        f"{delivery_id}:{attempts}"
        for delivery_id, attempts in due_deliveries.values_list("id", "attempts")
    ]
    if not components:
        raise ValueError("webhook retry command has no due deliveries")
    return f"retry:{'|'.join(components)}"


def _outbox_event_exists(message_id: uuid.UUID) -> bool:
    from escrow.messaging.models import OutboxEvent

    return OutboxEvent.objects.filter(id=message_id).exists()


def _new_signing_secret() -> str:
    return f"{_SECRET_PREFIX}{secrets.token_urlsafe(32)}"


def _secret_context(
    organization_id: uuid.UUID, endpoint_id: uuid.UUID, slot: str
) -> dict[str, str]:
    return {
        "service": "escrow",
        "purpose": "webhook-signing-secret",
        "organization_id": str(organization_id),
        "endpoint_id": str(endpoint_id),
        "slot": slot,
        "version": "1",
    }


def _decrypt_current_secret(endpoint: WebhookEndpoint) -> str:
    encrypted = EncryptedValue(
        ciphertext=bytes(endpoint.secret_ciphertext),
        nonce=bytes(endpoint.secret_nonce),
        encrypted_data_key=bytes(endpoint.secret_encrypted_data_key),
        kms_key_id=endpoint.secret_kms_key_id,
    )
    try:
        secret = (
            envelope_cipher()
            .decrypt(
                encrypted,
                _secret_context(endpoint.organization_id, endpoint.id, "current"),
            )
            .decode("ascii")
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise WebhookSecretUnavailable("webhook secret cannot be decrypted") from error
    if not secret.startswith(_SECRET_PREFIX):
        raise WebhookSecretUnavailable("webhook secret is invalid")
    return secret


def _decrypt_previous_secret(endpoint: WebhookEndpoint) -> str | None:
    if (
        endpoint.previous_secret_ciphertext is None
        or endpoint.previous_secret_nonce is None
        or endpoint.previous_secret_encrypted_data_key is None
        or not endpoint.previous_secret_kms_key_id
    ):
        return None
    encrypted = EncryptedValue(
        ciphertext=bytes(endpoint.previous_secret_ciphertext),
        nonce=bytes(endpoint.previous_secret_nonce),
        encrypted_data_key=bytes(endpoint.previous_secret_encrypted_data_key),
        kms_key_id=endpoint.previous_secret_kms_key_id,
    )
    try:
        secret = (
            envelope_cipher()
            .decrypt(
                encrypted,
                _secret_context(endpoint.organization_id, endpoint.id, "current"),
            )
            .decode("ascii")
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise WebhookSecretUnavailable("previous webhook secret cannot be decrypted") from error
    if not secret.startswith(_SECRET_PREFIX):
        raise WebhookSecretUnavailable("previous webhook secret is invalid")
    return secret


def _delivery_headers(
    event: WebhookEvent,
    endpoint: WebhookEndpoint,
    secret: str,
    body: bytes,
    now: datetime,
) -> dict[str, str]:
    timestamp = str(int(now.timestamp()))
    signed_payload = f"{timestamp}.".encode() + body
    signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Escrow-Webhook/1.0",
        "X-Escrow-Event": str(event.id),
        "X-Escrow-Timestamp": timestamp,
        "X-Escrow-Signature": f"v1={signature}",
    }
    if (
        endpoint.previous_secret_expires_at is not None
        and endpoint.previous_secret_expires_at > now
    ):
        previous_secret = _decrypt_previous_secret(endpoint)
        if previous_secret is not None:
            previous_signature = hmac.new(
                previous_secret.encode(), signed_payload, hashlib.sha256
            ).hexdigest()
            headers["X-Escrow-Previous-Signature"] = f"v1={previous_signature}"
    return headers


def _canonical_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _resolve_host(host: str) -> Sequence[str]:
    try:
        addresses: set[str] = set()
        for result in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
            address = result[4][0]
            if isinstance(address, str):
                addresses.add(address)
        return sorted(addresses)
    except OSError as error:
        raise WebhookEndpointValidationError("webhook endpoint cannot resolve") from error


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _endpoint_host(url: str) -> str:
    host = urlparse(url).hostname
    if host is None:
        raise WebhookEndpointValidationError("webhook endpoint is invalid")
    return host


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None


def _send_https_webhook(*, url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    """Send exactly one HTTPS request; redirects are errors, never followed."""
    validate_webhook_endpoint_url(url)
    request = urllib.request.Request(url=url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
