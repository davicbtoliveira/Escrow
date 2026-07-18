"""Operational probes for the composition root, with no domain side effects."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import pika
import redis
from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET

DependencyStatus = Literal["ok", "unavailable"]


def _database_is_available() -> bool:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:  # Connection details must never leave this operational probe.
        return False
    return True


def _rabbitmq_is_available() -> bool:
    try:
        parameters = pika.URLParameters(settings.RABBITMQ_URL)
        parameters.socket_timeout = settings.HEALTHCHECK_TIMEOUT_SECONDS
        parameters.connection_attempts = 1
        parameters.retry_delay = 0
        rabbit_connection = pika.BlockingConnection(parameters)
        rabbit_connection.close()
    except Exception:  # Connection details must never leave this operational probe.
        return False
    return True


def _redis_is_available() -> bool:
    try:
        client = redis.Redis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=settings.HEALTHCHECK_TIMEOUT_SECONDS,
            socket_timeout=settings.HEALTHCHECK_TIMEOUT_SECONDS,
        )
        client.ping()
    except Exception:  # Connection details must never leave this operational probe.
        return False
    return True


def _check(check: Callable[[], bool]) -> DependencyStatus:
    return "ok" if check() else "unavailable"


@require_GET
def liveness(_: object) -> JsonResponse:
    """Prove that the HTTP process is running without touching dependencies."""
    return JsonResponse({"status": "ok"})


@require_GET
def readiness(_: object) -> JsonResponse:
    """Report the independently checked local infrastructure state safely."""
    dependencies: dict[str, DependencyStatus] = {
        "database": _check(_database_is_available),
        "rabbitmq": _check(_rabbitmq_is_available),
        "redis": _check(_redis_is_available),
    }
    is_ready = all(status == "ok" for status in dependencies.values())
    return JsonResponse(
        {"status": "ready" if is_ready else "degraded", "dependencies": dependencies},
        status=200 if is_ready else 503,
    )
