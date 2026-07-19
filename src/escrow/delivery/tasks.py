"""Periodic commands owned by the delivery boundary."""

from __future__ import annotations

from celery import shared_task  # type: ignore[import-untyped]

from escrow.delivery.services import enqueue_expired_delivery_refunds


@shared_task(  # type: ignore[untyped-decorator]
    name="escrow.delivery.enqueue_expired_delivery_refunds"
)
def enqueue_expired_delivery_refunds_task() -> int:
    """Ask PostgreSQL which held agreements passed their delivery deadline."""
    return enqueue_expired_delivery_refunds()
