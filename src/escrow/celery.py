"""Celery composition root with ADR 0005's explicit transport guarantees."""

from __future__ import annotations

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "escrow.settings")

from celery import Celery  # type: ignore[import-untyped]

from escrow.messaging.topology import (
    ALL_QUEUES,
    CELERY_TASK_ROUTES,
    COMMANDS_EXCHANGE,
    OUTBOX_PUBLISHER_QUEUE,
)


def route_task(
    name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    options: dict[str, object],
    task: object | None = None,
    **extra: object,
) -> dict[str, str]:
    """Refuse accidental use of Celery's implicit ``celery`` queue."""
    del args, kwargs, options, task, extra
    try:
        return CELERY_TASK_ROUTES[name]
    except KeyError as error:
        raise ValueError(f"task {name!r} has no explicit route") from error


app = Celery("escrow")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.update(
    accept_content=("json",),
    broker_transport_options={"confirm_publish": True},
    result_backend=None,
    task_acks_late=True,
    task_create_missing_queues=False,
    task_default_delivery_mode=2,
    task_default_exchange=COMMANDS_EXCHANGE.name,
    task_default_queue=OUTBOX_PUBLISHER_QUEUE.name,
    task_default_routing_key=OUTBOX_PUBLISHER_QUEUE.name,
    task_ignore_result=True,
    task_publish_retry=True,
    task_publish_retry_policy={
        "max_retries": 5,
        "interval_start": 0,
        "interval_step": 1,
        "interval_max": 8,
    },
    task_queues=ALL_QUEUES,
    task_reject_on_worker_lost=True,
    task_routes=(route_task,),
    task_serializer="json",
    worker_prefetch_multiplier=1,
)
app.autodiscover_tasks()
