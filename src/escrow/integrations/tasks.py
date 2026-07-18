"""Periodic commands owned by the integration boundary."""

from __future__ import annotations

from celery import shared_task  # type: ignore[import-untyped]

from escrow.integrations.webhooks import enqueue_due_webhook_deliveries


@shared_task(  # type: ignore[untyped-decorator]
    name="escrow.integrations.enqueue_due_webhook_deliveries"
)
def enqueue_due_webhook_deliveries_task() -> int:
    """Ask PostgreSQL which endpoint deliveries are due for another attempt."""
    return enqueue_due_webhook_deliveries()
